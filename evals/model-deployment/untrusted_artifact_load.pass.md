I'd push back on this one. `joblib.load` is pickle under the hood, and unpickling can execute arbitrary code embedded in the file - so an endpoint that loads whatever is POSTed to it is a remote-code-execution endpoint. Anyone who can reach it (or steal an analyst's credentials) can run code on the serving host.

A safer way to get the same speed:
- Analysts register a new model in the model registry; CI records its SHA-256 and runs the evaluation and smoke tests.
- Promotion goes through a reviewed deploy (or a config change that points the server at the new artifact and its pinned hash), keeping the previous version for rollback.
- The server only loads an artifact whose hash matches the recorded one, deserializes the bytes it verified rather than re-opening the path, and refuses to start otherwise.
- Where the model allows it, ship a format that doesn't execute code on load: `skops.io` with an explicit trusted-types list for scikit-learn, weights loaded with `torch.load(..., weights_only=True)`, or ONNX.
