---
name: fine-tuning-llms
display_name: Fine-Tuning LLMs
description: >
  Use when the user wants to adapt a pretrained LLM's weights to a specific task
  or domain — preparing an instruction dataset and training a LoRA/QLoRA adapter
  with PEFT rather than prompting or retrieving. Trigger phrases: "fine-tune this
  model", "train a LoRA adapter", "QLoRA setup", "prep a dataset for
  instruction tuning", "why is my fine-tuned model overfitting". NOT for
  steering behavior via the prompt alone (see prompt-engineering), grounding
  answers in a document corpus at inference time (see rag-pipeline), or the
  transformer/attention internals being adapted (see attention-mechanisms).
type: workflow
domain: llm
level: advanced
lifecycle: stable
risk_level: medium
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - lora-adapter-training
  - qlora-quantization
  - instruction-dataset-prep
  - peft-configuration
  - adapter-merging
requires:
conflicts:
related:
  - attention-mechanisms
  - prompt-engineering
  - rag-pipeline
  - llm-evaluation
  - training-deep-models
inputs: An instruction dataset (prompt/response pairs) and a pretrained base model to adapt.
outputs: A saved LoRA adapter (and optionally a merged standalone model) trained toward the target task/domain/format.
version_constraints:
  - "peft>=0.21: LoraConfig / get_peft_model / prepare_model_for_kbit_training / PeftModel.from_pretrained().merge_and_unload() (executed against peft 0.21.2 on 2026-10-27 with a tiny local Llama; older releases not tested)"
  - "trl>=1.16: SFTConfig(max_length=, eval_strategy=) and SFTTrainer(processing_class=) with a prompt/completion dataset; SFTConfig(max_seq_length=) raises TypeError on 1.16 (executed against trl 1.16.0 on 2026-10-27; older releases not tested)"
  - "transformers>=5.21: BitsAndBytesConfig construction and from_pretrained(dtype=) (executed against transformers 5.21.0 on 2026-10-27 with from_pretrained stubbed; real 4-bit loading needs CUDA + bitsandbytes and was not executed)"
  - "datasets>=5.1: load_dataset(json) / Dataset.map / train_test_split (executed against datasets 5.1.0 on 2026-10-27; older releases not tested)"
---

## Overview
Fine-tuning updates an LLM's weights (or a small set of added weights) so behavior
that a prompt alone can't reliably produce — a house style, a domain vocabulary,
a strict output format — becomes baked in. This skill covers the practical,
parameter-efficient path: dataset prep, QLoRA (4-bit base model + LoRA adapters)
via Hugging Face `transformers`/`peft`/`bitsandbytes`, and saving/merging the
result. It assumes you already have a reason to fine-tune rather than prompt or
retrieve — if you don't, `prompt-engineering` or `rag-pipeline` are cheaper first.

## Workflow
1. **Prepare the instruction dataset.** Format examples as consistent
   prompt/completion pairs using the same template the base model was
   instruction-tuned with (or a fixed custom template if training from a plain
   base model), and hold out an eval split before training. With `prompt` /
   `completion` columns, TRL's `SFTTrainer` computes the loss on the completion
   tokens only and appends the EOS token, so the model learns to answer and stop
   rather than to reproduce prompts.
   ```python
   from datasets import load_dataset

   raw = load_dataset("json", data_files="train.jsonl")["train"]
   # Each record: {"instruction": ..., "input": ... (may be empty/absent), "output": ...}

   def to_prompt_completion(example):
       prompt = f"### Instruction:\n{example['instruction']}\n\n"
       if (example.get("input") or "").strip():  # keep the input; dropping it trains on half the task
           prompt += f"### Input:\n{example['input']}\n\n"
       prompt += "### Response:\n"
       return {"prompt": prompt, "completion": example["output"]}

   dataset = raw.map(to_prompt_completion, remove_columns=raw.column_names)
   splits = dataset.train_test_split(test_size=0.1, seed=42)  # held-out eval split
   train_ds, eval_ds = splits["train"], splits["test"]
   ```
2. **Load the base model in 4-bit (QLoRA) and its tokenizer.** Quantizing the
   frozen base weights to 4-bit slashes memory so larger models fit on a single
   GPU; only the LoRA adapters train in higher precision. 4-bit loading needs a
   CUDA GPU with `bitsandbytes` installed, and gated checkpoints such as Llama
   need an accepted licence plus a Hugging Face access token.
   ```python
   import torch
   from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

   model_id = "meta-llama/Llama-3.1-8B"
   bnb_config = BitsAndBytesConfig(
       load_in_4bit=True,
       bnb_4bit_quant_type="nf4",
       bnb_4bit_compute_dtype=torch.bfloat16,
       bnb_4bit_use_double_quant=True,
   )
   tokenizer = AutoTokenizer.from_pretrained(model_id)
   tokenizer.pad_token = tokenizer.pad_token or tokenizer.eos_token

   model = AutoModelForCausalLM.from_pretrained(
       model_id, quantization_config=bnb_config, device_map="auto"
   )
   ```
3. **Wrap the model with a LoRA adapter via PEFT.** Prepare the quantized model
   for k-bit training first, then attach low-rank adapters to the attention
   projection layers.
   ```python
   from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

   model = prepare_model_for_kbit_training(model)

   lora_config = LoraConfig(
       r=16, lora_alpha=32, lora_dropout=0.05,
       target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],  # model-specific
       task_type="CAUSAL_LM",
   )
   model = get_peft_model(model, lora_config)
   model.print_trainable_parameters()  # sanity-check: should be << total params
   ```
4. **Train with `SFTTrainer` (TRL).** Handles tokenization, completion-only
   loss masking, padding, and the training loop; evaluating on the held-out split
   each epoch is what makes overfitting visible.
   ```python
   from trl import SFTTrainer, SFTConfig

   sft_config = SFTConfig(
       output_dir="./lora-out",
       per_device_train_batch_size=4,
       gradient_accumulation_steps=4,
       num_train_epochs=3,
       learning_rate=2e-4,          # LoRA tolerates/needs a higher LR than full FT
       bf16=True,                   # needs a bf16-capable GPU; use fp16=True on older GPUs
       logging_steps=10,
       eval_strategy="epoch",
       save_strategy="epoch",
       max_length=1024,             # older TRL releases called this max_seq_length
   )
   trainer = SFTTrainer(
       model=model,
       args=sft_config,
       train_dataset=train_ds,
       eval_dataset=eval_ds,
       processing_class=tokenizer,  # reuse the step-2 tokenizer (and its pad token)
   )
   trainer.train()
   ```
5. **Save the adapter, and optionally merge it into the base model.** Saving the
   adapter alone keeps it small (MBs) and swappable; merging produces a single
   deployable model at the cost of losing that flexibility. Merge into a freshly
   loaded 16-bit base: `model` is already a `PeftModel` (wrapping it again nests the
   wrappers and `merge_and_unload()` then raises), and merging into the 4-bit
   training base bakes quantization rounding error into the merged weights (PEFT
   warns about this). The 16-bit base needs roughly 2 bytes per parameter of
   memory; if the training GPU cannot hold it even after freeing the training
   objects, run this merge as a separate script on a machine that can (it only
   needs `model_id` and the saved adapter directory).
   ```python
   adapter_dir = "./lora-out/adapter"
   model.save_pretrained(adapter_dir)       # small, swappable adapter only
   tokenizer.save_pretrained(adapter_dir)

   # Optional: merge for a standalone deployable model
   import gc
   from peft import PeftModel

   # The adapter is on disk now. Drop the 4-bit model, trainer and optimizer state before
   # loading a second, 16-bit copy of the base (~16 GB for an 8B model); otherwise the
   # reload can OOM, or device_map="auto" quietly offloads layers to CPU/disk.
   del trainer, model
   gc.collect()
   torch.cuda.empty_cache()  # no-op when CUDA was never initialised

   base = AutoModelForCausalLM.from_pretrained(model_id, dtype=torch.bfloat16, device_map="auto")
   merged = PeftModel.from_pretrained(base, adapter_dir).merge_and_unload()
   merged.save_pretrained("./lora-out/merged-model")
   tokenizer.save_pretrained("./lora-out/merged-model")
   ```

## Gotchas
- **`target_modules` must match the actual layer names for the model family.**
  `q_proj`/`v_proj` works for Llama-style models but not every architecture —
  inspect `model.named_modules()` first if adapters silently attach to nothing.
- **Skipping `prepare_model_for_kbit_training`** before wrapping with LoRA breaks
  gradient flow through the quantized base (frozen layers need casting/gradient
  checkpointing set up correctly) — training will run but barely learn.
- **LoRA needs a higher learning rate than full fine-tuning** (typically 1e-4 to
  2e-4 vs. 1e-5 to 5e-5) because only a small fraction of parameters are updated;
  reusing a full-FT LR under-trains the adapter.
- **Overfitting on small instruction sets.** LoRA's low parameter count reduces
  but does not eliminate overfitting risk on datasets under a few thousand
  examples — hold out an eval split and watch validation loss, don't just trust
  training loss going down.
- **Tokenizer padding side for causal LMs.** Training typically pads right, but
  batched generation afterward needs left-padding — using the wrong side for the
  wrong phase produces garbled outputs or silently wrong loss masking.
- **Library argument renames break copied recipes.** On TRL 1.16,
  `SFTConfig(max_seq_length=...)` raises `TypeError` (use `max_length`), and
  Transformers 5 accepts `eval_strategy`, not `evaluation_strategy` — check the
  installed versions before reusing an older notebook.
- **Forgetting to freeze/quantize correctly leads to OOM.** 4-bit quantization
  only reduces the *base* model's memory; gradient accumulation, optimizer states
  for the adapter, and activation memory (mitigated by gradient checkpointing)
  still need to fit — profile before scaling batch size up.

## References
- [Hu et al., 2021 — LoRA: Low-Rank Adaptation of Large Language Models](https://arxiv.org/abs/2106.09685) — the adapter method used throughout this workflow.
- [Dettmers et al., 2023 — QLoRA: Efficient Finetuning of Quantized LLMs](https://arxiv.org/abs/2305.14314) — the 4-bit quantization + LoRA combination in steps 2–3.
- [Hugging Face PEFT documentation](https://huggingface.co/docs/peft/index) — `LoraConfig`, `get_peft_model`, and merging APIs used above.
