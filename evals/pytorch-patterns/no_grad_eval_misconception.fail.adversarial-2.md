no_grad handles this. BatchNorm and dropout only matter in training because they need gradients. Look at your data loader shuffle and seeds instead; eval mode won't make a difference here.
