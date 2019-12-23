import os
import random
import torch
import numpy as np
import glob
import PIL.Image as Image
import pandas as pd
import cv2
from torch.utils.data import Dataset


class ImageNet_A(Dataset):
	def __init__(self, root_dir):
		labels_dir = os.path.join(root_dir, 'dev.csv')
		self.image_dir = os.path.join(root_dir, 'images')
		self.labels = pd.read_csv(labels_dir)

	def __len__(self):
		l = len(self.labels)
		return l

	def __getitem__(self, idx):
		filename = os.path.join(self.image_dir, self.labels.at[idx, 'ImageId'])
		in_img_t = cv2.imread(filename)[:, :, ::-1]
		
		in_img = np.transpose(in_img_t.astype(np.float32), axes=[2, 0, 1])
		img = in_img / 255.0

		label_true = self.labels.at[idx, 'TrueLabel']
		label_target = self.labels.at[idx, 'TargetClass']

		return img, label_true, label_target, filename
