Nope. The schedule is indexed by batches, so call scheduler.step() for each batch. With per-epoch calls the learning rate never ramps up - after 20 calls it's 20/500 of its target.
