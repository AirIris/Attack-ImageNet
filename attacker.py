from typing import Optional, Tuple
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np

def gkern(kernlen=21, nsig=3, device=torch.device('cpu')):
    import scipy.stats as st

    x = np.linspace(-nsig, nsig, kernlen)
    kern1d = st.norm.pdf(x)
    kernel_raw = np.outer(kern1d, kern1d)
    kernel = (kernel_raw / kernel_raw.sum()).astype(np.float32)

    stack_kernel = np.stack([kernel, kernel, kernel])
    stack_kernel = np.expand_dims(stack_kernel, 1)
    stack_kernel = torch.Tensor(stack_kernel).to(device)

    return stack_kernel


class tv_loss(nn.Module):
    def __init__(self):
        super().__init__()
        
    def forward(self, noise):
        h_x = noise.size()[2]
        w_x = noise.size()[3]
        count_h = self._tensor_size(noise[:, :, 1:, :])
        count_w = self._tensor_size(noise[:, :, : ,1:])
        h_tv = torch.pow((noise[:, :, 1:, :] - noise[:, :, :h_x-1, :]), 2).sum()
        w_tv = torch.pow((noise[:, :, :, 1:] - noise[:, :, :, :w_x-1]), 2).sum()
        tvloss = h_tv / count_h + w_tv / count_w

        return tvloss

    def _tensor_size(self,t):
        return t.size()[1]*t.size()[2]*t.size()[3]


class Attacker:
    def __init__(self,
                 steps: int,
                 gamma: float = 0.05,
                 init_norm: float = 1.,
                 quantize: bool = True,
                 levels: int = 256,
                 max_norm: Optional[float] = None,
                 min_loss: Optional[float] = None,
                 device: torch.device = torch.device('cpu')) -> None:
        self.steps = steps
        self.gamma = gamma
        self.init_norm = init_norm

        self.quantize = quantize
        self.levels = levels
        self.max_norm = max_norm
        self.min_loss = min_loss
        
        self.device = device

    def _iter_attack(self, 
                     model1: nn.Module, 
                     model2: nn.Module,
                     model3: nn.Module,
                     inputs: torch.Tensor, 
                     labels: torch.Tensor,
                     epsilon: Optional[float] = None,
                     targeted: bool = False,
                     strict: bool = False)-> torch.Tensor:

        batch_size = inputs.shape[0]
        multiplier = 1 if targeted else -1
        delta = torch.zeros_like(inputs, requires_grad=True)
        norm = torch.full((batch_size,), self.init_norm, device=self.device, dtype=torch.float)

        # Setup optimizers
        optimizer = optim.Adam([delta], lr=1)
        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=int(self.steps/3), gamma=0.1)

        # for choosing best results
        best_loss = 1e4 * torch.ones(inputs.size(0), dtype=torch.float, device=self.device)
        best_delta = torch.zeros_like(inputs)

        adv_found = torch.zeros(inputs.size(0), dtype=torch.uint8, device=self.device)

        for _ in range(self.steps):
            if epsilon:
                # delta.data.renorm_(p=float('inf'), dim=0, maxnorm=epsilon)
                delta.data.clamp_(-epsilon, epsilon)
                if self.quantize:
                    delta.data.mul_(self.levels - 1).round_().div_(self.levels - 1)

            adv = inputs + delta

            for _, param in enumerate(model1.parameters()):
                param.requires_grad = False
            for _, param in enumerate(model2.parameters()):
                param.requires_grad = False

            logits1 = model1(adv)
            logits2 = model2(adv)
            logits3 = model3(adv)

            ce_loss1 = F.cross_entropy(logits1, labels, reduction='none')
            ce_loss2 = F.cross_entropy(logits2, labels, reduction='none')
            ce_loss3 = F.cross_entropy(logits3, labels, reduction='none')

            loss = (ce_loss1 + ce_loss2 + ce_loss3) * multiplier

            pred_labels1 = logits1.argmax(1)
            pred_labels2 = logits2.argmax(1)
            pred_labels3 = logits3.argmax(1)

            is_adv = ((pred_labels1 == labels) * (pred_labels2 == labels) * (pred_labels3 == labels)) if targeted \
                else ((pred_labels1 != labels) * (pred_labels2 != labels) * (pred_labels3 != labels))
            
            is_better = loss < best_loss
            is_both = is_adv * is_better
            adv_found = (adv_found + is_both) > 0

            if strict:
                best_loss[is_both] = loss[is_both]
                best_delta[is_both] = delta.data[is_both]
            else:
                best_loss[is_better] = loss[is_better]
                best_delta[is_better] = delta.data[is_better]
            
            loss = torch.mean(loss)
            optimizer.zero_grad()
            loss.backward()

            # renorming gradient to [-1, 1]
            grad_norms = delta.grad.view(batch_size, -1).norm(p=float('inf'), dim=1)
            delta.grad.div_(grad_norms.view(-1, 1, 1, 1))

            # avoid nan or inf if gradient is 0
            if (grad_norms == 0).any():
                delta.grad[grad_norms == 0] = torch.randn_like(delta.grad[grad_norms == 0])

            # # Gaussian Process
            # k_size = 11
            # g_kernel = gkern(k_size, 3, self.device)
            # delta.grad = F.conv2d(delta.grad, g_kernel, padding=(k_size-1)//2, groups=3)

            optimizer.step()

            # DDN speeder
            norm.mul_(1 - (2 * is_adv.float() - 1) * self.gamma)
            delta.data.mul_((norm / delta.data.view(batch_size, -1).norm(p=float('inf'), dim=1)).view(-1, 1, 1, 1))

            # avoid out of bound
            delta.data.add_(inputs)
            delta.data.clamp_(0, 1).sub_(inputs)

            scheduler.step()

        return best_delta, adv_found, best_loss

    def attack(self, 
               model1: nn.Module, 
               model2: nn.Module,
               model3: nn.Module,
               inputs: torch.Tensor, 
               labels: torch.Tensor,
               targeted: bool = False,
               strict: bool = False)-> torch.Tensor:

        if inputs.min() < 0 or inputs.max() > 1: raise ValueError('Input values should be in the [0, 1] range.')

        best_delta, _, best_loss = self._iter_attack(model1, model2, model3, inputs, labels, self.max_norm, targeted, strict)

        if self.min_loss:
            if ((-best_loss) <= self.min_loss).all():
                return inputs + best_delta

            epsilons = np.arange(self.max_norm / 32, self.max_norm + self.max_norm / 32, self.max_norm / 32)

            index_min = 0
            index_max = len(epsilons) - 1
            for _ in range(len(epsilons)):
                
                index_mid = int((index_min + index_max) / 2)
                epsilon = epsilons[index_mid]

                best_delta_t, _, best_loss = self._iter_attack(model1, model2, model3, inputs, labels, epsilon, targeted, strict)

                if ((-best_loss) > self.min_loss).all():
                    best_delta = best_delta_t
                    index_max = index_mid - 1
                else:
                    index_min = index_mid + 1

                if index_min >= index_max:
                    return inputs + best_delta

        return inputs + best_delta
