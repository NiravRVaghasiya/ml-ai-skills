conv1 gives a 112x112 map: floor((224+6-7)/2)+1. For the receptive field, multiply each layer's (k-1) by the product of the earlier strides: 1+6=7, 7+2*2=11, 11+2*4 gives 19x19 pixels.
