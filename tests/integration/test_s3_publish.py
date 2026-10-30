"""Opt-in: data-pipelines step 1 publishing to ``s3://``, against a local moto S3 server.

Gate: ``ML_AI_SKILLS_INTEGRATION_S3=1``. Needs ``moto[server]``, ``s3fs``, pandas, pyarrow and
SQLAlchemy (none of moto/s3fs is in requirements-examples.txt).

What it tests: with ``FEATURE_PIPELINE_DATA_DIR=s3://<bucket>/pipeline`` the step-1 block runs
verbatim; the ``"://"`` guard keeps ``load()`` from calling ``os.makedirs`` on the URL, the
published object has the columns the step-3 Feast ``FileSource`` reads, and a retried load
overwrites the one object per partition (the same checks as the offline ``memory://`` test,
``tests/examples/test_data_pipelines_object_store.py``).

Which S3 client does the skill's I/O: the skill calls ``to_parquet``/``read_parquet`` on the URL
without ``storage_options``. pandas 2.2 (``pandas.io.parquet._get_path_or_handle``) then tries
``pyarrow.fs.FileSystem.from_uri`` first and falls back to fsspec only if that fails, so with a
pyarrow build that has S3 support the skill's reads and writes go through **pyarrow's native
(C++ AWS SDK) S3 client**, which finds moto through ``AWS_ENDPOINT_URL``. That is the default
case below, and the test asserts the route rather than assuming it. A second case forces the
fsspec fallback (as if pyarrow had no S3 support, or the caller passed ``storage_options``), so
the same skill code also runs through **s3fs -> aiobotocore**. In both cases the test's own
listing and read-back use s3fs with an explicit endpoint.

Against what: moto's ``ThreadedMotoServer`` bound to 127.0.0.1 on a free port for the lifetime of
the class, never a real bucket. The class points pyarrow/s3fs/botocore at that endpoint, sets
throwaway credentials, and removes ``AWS_PROFILE`` and the shared config/credentials files, so a
real credential chain can never be picked up. Every patch is registered with
``addClassCleanup`` as soon as it is applied, so a failure part-way through ``setUpClass`` does
not leak it into the rest of the process. Not covered: IAM policies, real S3
consistency/latency, KMS, multi-region.
"""
from __future__ import annotations

import inspect
import logging
import os
import socket
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from examples import test_data_pipelines_object_store as offline
from integration import gate


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _restore_fsspec_s3(previous):
    import fsspec.config

    if previous is None:
        fsspec.config.conf.pop("s3", None)
    else:
        fsspec.config.conf["s3"] = previous


@gate.opt_in("S3", what="data-pipelines s3:// publish path (pyarrow S3 and s3fs) against a local moto S3 server",
             modules=("moto", "moto.server", "s3fs", "pandas", "pyarrow", "pyarrow.fs", "sqlalchemy", "flask"))
class TestPublishToS3ViaMoto(offline.ObjectStoreFlowMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from moto.server import ThreadedMotoServer

        cls.port = _free_loopback_port()
        cls.endpoint = f"http://127.0.0.1:{cls.port}"
        cls._cfg = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._cfg.cleanup)
        missing = Path(cls._cfg.name) / "absent"   # no real profile/credentials file is ever read
        env = mock.patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL": cls.endpoint, "AWS_ENDPOINT_URL_S3": cls.endpoint,
            "FSSPEC_S3_ENDPOINT_URL": cls.endpoint,
            "AWS_SHARED_CREDENTIALS_FILE": str(missing), "AWS_CONFIG_FILE": str(missing),
            "AWS_EC2_METADATA_DISABLED": "true",
        })
        env.start()
        cls.addClassCleanup(env.stop)
        for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):   # restored by env.stop()
            os.environ.pop(name, None)
        # FSSPEC_S3_* env vars are read when fsspec is imported, which may already have
        # happened; set the same default in the live config.
        import fsspec.config

        previous = fsspec.config.conf.get("s3")
        fsspec.config.conf["s3"] = {**(previous or {}), "endpoint_url": cls.endpoint}
        cls.addClassCleanup(_restore_fsspec_s3, previous)
        logging.getLogger("werkzeug").setLevel(logging.ERROR)   # one access-log line per S3 request
        cls.server = ThreadedMotoServer(ip_address="127.0.0.1", port=cls.port, verbose=False)
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)

    def setUp(self):
        import s3fs

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.workdir = Path(self._tmp.name)
        self.db_url = f"sqlite:///{(self.workdir / 'warehouse.db').as_posix()}"
        offline.make_warehouse(self.db_url)
        s3fs.S3FileSystem.clear_instance_cache()
        self.fs = s3fs.S3FileSystem(client_kwargs={"endpoint_url": self.endpoint})
        self.assertTrue(self.fs.client_kwargs["endpoint_url"].startswith("http://127.0.0.1:"))
        bucket = f"mlai-{uuid.uuid4().hex[:12]}"
        self.fs.mkdir(bucket)
        self.data_dir = f"s3://{bucket}/pipeline"
        old_cwd = os.getcwd()
        os.chdir(self.workdir)
        self.addCleanup(os.chdir, old_cwd)

    def object_names(self, fs, prefix):
        fs.invalidate_cache()
        return sorted(p.rsplit("/", 1)[-1] for p in fs.ls(prefix, detail=False) if not fs.isdir(p))

    def record_parquet_routes(self, *, force_fsspec: bool) -> list[tuple[str, str, bool, object]]:
        """Wrap pandas' parquet path resolver; return a live list of
        ``(path, mode, caller_passed_storage_options, filesystem)`` per to_parquet/read_parquet call.

        ``force_fsspec`` turns a missing ``storage_options`` into ``{}``: pandas then skips
        ``pyarrow.fs.FileSystem.from_uri`` and resolves the URL with fsspec (s3fs), exactly as
        it does when from_uri fails."""
        import pandas.io.parquet as pq

        original = getattr(pq, "_get_path_or_handle", None)
        if original is None:   # opted in: an unrecognised pandas is a failure, not a skip
            import pandas as pd

            self.fail(f"pandas {pd.__version__} has no pandas.io.parquet._get_path_or_handle; "
                      "update how this test observes the S3 client")
        signature = inspect.signature(original)
        calls: list[tuple[str, str, bool, object]] = []

        def resolve(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            given = bound.arguments["storage_options"] is not None
            if force_fsspec and not given:
                bound.arguments["storage_options"] = {}
            out = original(*bound.args, **bound.kwargs)
            calls.append((str(bound.arguments["path"]), bound.arguments["mode"], given, out[2]))
            return out

        patch = mock.patch.object(pq, "_get_path_or_handle", resolve)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def run_flow_and_check_route(self, *, force_fsspec: bool, expected_type: type):
        import pandas as pd

        calls = self.record_parquet_routes(force_fsspec=force_fsspec)

        def read(url):   # read back with an explicit endpoint, independent of the env lookup
            return pd.read_parquet(url, storage_options={"client_kwargs": {"endpoint_url": self.endpoint}})

        self.check_flow(self.fs, read)
        skill_calls = [(path, mode, fs) for path, mode, given, fs in calls if not given]
        # extract writes orders; transform reads orders, writes features; load reads features,
        # writes the published object; the retried load reads and writes again.
        self.assertGreaterEqual(len(skill_calls), 7, calls)
        self.assertEqual({mode for _, mode, _ in skill_calls}, {"rb", "wb"}, calls)
        for path, mode, fs in skill_calls:
            self.assertTrue(path.startswith(self.data_dir), path)
            self.assertIsInstance(fs, expected_type, f"{mode} {path} went through {type(fs).__module__}."
                                                     f"{type(fs).__qualname__}")
        return skill_calls

    def test_extract_transform_load_publish_to_s3_through_pyarrow_s3(self):
        """Default route for the skill's code: pyarrow's native S3 filesystem."""
        try:   # pyarrow.fs raises ImportError (not AttributeError) for a component not built
            from pyarrow.fs import S3FileSystem
        except ImportError as exc:
            self.fail(f"this pyarrow build has no S3 support ({exc}); pandas would use s3fs "
                      "instead, which the fsspec case covers")
        self.run_flow_and_check_route(force_fsspec=False, expected_type=S3FileSystem)

    def test_extract_transform_load_publish_to_s3_through_s3fs_fallback(self):
        """pandas' fsspec fallback for the same skill code: s3fs."""
        import s3fs

        self.run_flow_and_check_route(force_fsspec=True, expected_type=s3fs.S3FileSystem)


if __name__ == "__main__":
    unittest.main()
