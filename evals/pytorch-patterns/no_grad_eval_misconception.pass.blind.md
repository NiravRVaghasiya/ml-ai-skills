Yes you need it. no_grad disables autograd; eval() switches dropout off and makes batchnorm use running estimates.
