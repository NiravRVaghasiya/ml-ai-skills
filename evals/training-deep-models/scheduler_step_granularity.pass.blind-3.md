No. The lambda is written in batches, so the warmup alone is 500 steps but you only call it 20 times - the warmup never completes. Call scheduler.step() right after every optimizer.step().
