CUDA_VISIBLE_DEVICES=0,1 python ../train.py \
--checkname testing \
--network searched_dense \
--batch-size 16 \
--workers 16 \
--dataset cityscapes \
--use-balanced-weights \
--epoch 2150 \
--lr 0.05 \
--nesterov \
--gpu-ids 0,1 \
--saved-arch-path ../searched_arch/40_5e_38_lr
