No. Since total_steps counts batches, call scheduler.step() after every optimizer step. Right now the schedule has barely started its warmup after 20 epochs, so your LR is close to zero.
