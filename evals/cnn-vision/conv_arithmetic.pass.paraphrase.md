**conv1 output**: (224 + 2·3 − 7)/2 + 1 = 111.5 → floor gives 111 + 1... let me redo: floor((224 + 6 − 7)/2) + 1 = floor(111.5) + 1 = 112. So 112×112.

**Receptive field** (r = r_prev + (k − 1)·j, where j is the jump, i.e. the product of strides so far):
| layer | k | s | j before | r |
|---|---|---|---|---|
| input | | | | 1 |
| conv1 | 7 | 2 | 1 | 7 |
| conv2 | 3 | 2 | 2 | 11 |
| conv3 | 3 | 1 | 4 | 19 |

Each conv3 output sees a 19×19 region of the image.
