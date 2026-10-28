No. LambdaLR counts calls, and your lambda is in batches, so after 20 epochs you've done 20 of 500 warmup steps; the LR is 4% of peak. Step the scheduler after every optimizer.step().
