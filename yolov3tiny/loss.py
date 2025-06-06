from typing import Tuple, List

import torch
import torch.nn.functional as F

def iou(bboxes1: torch.Tensor, bboxes2: torch.Tensor, center_aligned=False, center_format=False):
    """ 
        Calculate the intersection / union between boxes1 and boxes2 (per batch)

        bboxes1, bboxes2 shape: (batch_size, number_of_boxes, 4)

        center_aligned: boxes center are aligned, useful for getting iou of anchor boxes to choose best one.
        center_format: are boxes coords in format (cx, cy, w, h) or x,y in top left.

        output: (batch_size, n1, n2)
    """

    x1, x2 = bboxes1[..., 0], bboxes2[..., 0]
    y1, y2 = bboxes1[..., 1], bboxes2[..., 1]
    w1, w2 = bboxes1[..., 2], bboxes2[..., 2]
    h1, h2 = bboxes1[..., 3], bboxes2[..., 3]

    area1 = (w1 * h1).unsqueeze(2)
    area2 = (w2 * h2).unsqueeze(1)

    if center_aligned:
        w = torch.minimum(w1.unsqueeze(2), w2.unsqueeze(1)).clamp(min=0)
        h = torch.minimum(h1.unsqueeze(2), h2.unsqueeze(1)).clamp(min=0)
    else:
        if center_format:
            x1 = x1 - w1 / 2
            x2 = x2 - w2 / 2

            y1 = y1 - h1 / 2
            y2 = y2 - h2 / 2

        right1 = (x1 + w1).unsqueeze(2)
        right2 = (x2 + w2).unsqueeze(1)
        left1 = x1.unsqueeze(2)
        left2 = x2.unsqueeze(1)
        w = (torch.minimum(right1, right2) - torch.maximum(left1, left2)).clamp(min=0)

        top1 = y1.unsqueeze(2)
        top2 = y2.unsqueeze(1)
        bottom1 = (y1 + h1).unsqueeze(2)
        bottom2 = (y2 + h2).unsqueeze(1)
        h = (torch.minimum(bottom1, bottom2) - torch.maximum(top1, top2)).clamp(min=0)

    intersection = w * h

    return intersection / (area1 + area2 - intersection + 1e-9) # might not need epsilon


def no_object_mask(pred: torch.Tensor, target: torch.Tensor, ignore_thresh:int):
    """
        Creates a mask, same shape as pred without box dims (batch_size, num_predictions),
        1 == no prediction with a good iou in target
        0 otherwise
    """
    batch_size, _, num_attributes = pred.shape
    assert batch_size == target.shape[0], num_attributes == target.shape[2]
    ious = iou(pred[..., :4], target[..., :4], center_format=True)
    return torch.max(ious, dim=2)[0] < ignore_thresh 

def no_object_mask_filter(noobj_mask: torch.Tensor, object_index: torch.Tensor):
    """
        noobj_mask: mask, 1 == no prediction with decent iou in targets
        object_index: indices where theres an object (in target)
    """
    batch_size, num_predictions = noobj_mask.shape
    noobj_mask = noobj_mask.view(-1) # flatten
    _filter = torch.zeros(noobj_mask.shape, device=noobj_mask.device)
    noobj_mask.scatter_(0, object_index, _filter)
    return noobj_mask.view(batch_size, num_predictions)

# TODO: should work for variable sized image_size so that people can train smaller models faster
def preprocess_targets(target_batch: torch.Tensor, num_targets: torch.Tensor,
                       list_anchors: List[Tuple[int, int]], image_size:int):
    """
        Converting the target boxes into the form the model predicts.
        x,y should be between (image_size, image_size)

        bx = sigmoid(tx) + cx, tx = logit(bx-cx). we want tx. same for y.
        bw = pw * e^tw, tw = ln(bw/pw). we want tw. same for box height
    """

    wh_anchors = torch.tensor(list_anchors).to(target_batch.device).float()
    xy_anchors = torch.zeros(wh_anchors.shape, device=target_batch.device)
    bbox_anchors = torch.cat((xy_anchors, wh_anchors), dim=1).unsqueeze(0)

    iou_anchors_target = iou(bbox_anchors, target_batch[..., :4], center_aligned=True)
    anchor_index = torch.argmax(iou_anchors_target, dim=1)

    scale = anchor_index // 3 # 0 or 1
    cell_size = image_size / 26 # 416 / 26 = 16
    stride = (cell_size * 2 ** scale).float() # 16 for upsampled (smaller boxes), 32 otherwise

    cx = target_batch[..., 0] // stride # which grid cell?
    cy = target_batch[..., 1] // stride

    tx = (target_batch[..., 0] / stride - cx).clamp(1e-9, 1-1e-9) # need to clamp so logit function works
    tx = torch.log(tx / (1 - tx))

    ty = (target_batch[..., 1] / stride - cy).clamp(1e-9, 1-1e-9)
    ty = torch.log(ty / (1 - ty))

    chosen_anchors = torch.index_select(wh_anchors, 0, torch.flatten(anchor_index)).reshape(tuple(list(anchor_index.shape) + [2])) # make this better
    tw = torch.log(target_batch[..., 2] / chosen_anchors[..., 0])
    th = torch.log(target_batch[..., 3] / chosen_anchors[..., 1])

    target_batch_t = target_batch.clone().detach()

    target_batch_t[..., 0] = tx
    target_batch_t[..., 1] = ty
    target_batch_t[..., 2] = tw
    target_batch_t[..., 3] = th

    large_scale_mask = scale.logical_not().long()
    grid_size = image_size // stride

    object_index = (large_scale_mask * (image_size // 32) ** 2 * 3 \
            + grid_size ** 2 * (anchor_index % 3) \
            + grid_size * cy \
            + cx).long()

    object_index_flat = []
    targets_flat = []

    batch_size = target_batch.shape[0]
    num_predictions = 13 ** 2 * 3 + 26 ** 2 * 3

    for batch in range(batch_size):
        indices = object_index[batch]
        targets = target_batch_t[batch]
        length = num_targets[batch]
        object_index_flat.append(indices[:length] + batch * num_predictions)
        targets_flat.append(targets[:length])

    return torch.cat(targets_flat), torch.cat(object_index_flat)

class YOLOLoss(torch.nn.Module):
    def __init__(self, anchors, image_size, ignore_thresh, no_object_coeff, coord_coeff):
        super().__init__()
        self.anchors = anchors
        self.image_size = image_size
        self.ignore_thresh = ignore_thresh
        self.no_object_coeff = no_object_coeff
        self.coord_coeff = coord_coeff

    def forward(self, predictions: torch.Tensor, targets: torch.Tensor, num_targets: torch.Tensor):
        targets, target_indices = preprocess_targets(targets, num_targets, self.anchors, self.image_size)
        noobj_mask = no_object_mask(predictions, targets, self.ignore_thresh)
        noobj_mask = no_object_mask_filter(noobj_mask, target_indices)

        # Loss
        # 1. no object loss: supress false positives
        # 2. object loss: suppress false negatives
        # 3. class loss
        # 4. coord loss
        # 5. loss = no object loss + object loss + class loss + coord loss

        # no objectness loss
        confidence_logits = predictions[..., 4]
        target_confidence_noobj = torch.zeros_like(confidence_logits)
        noobj_confidence_logits = confidence_logits - (1 - noobj_mask) * 1e7
        noobj_loss = F.binary_cross_entropy_with_logits(noobj_confidence_logits, target_confidence_noobj, reduction="sum")

        batch_size, num_predictions, _ = predictions.shape
        preds_obj = predictions.view(batch_size * num_predictions, -1).index_select(0, target_indices)

        coord_loss = F.mse_loss(preds_obj[..., :4], targets[..., :4], reduction="sum")

        target_confidence_obj = torch.ones_like(preds_obj[..., 4])
        obj_loss = F.binary_cross_entropy_with_logits(preds_obj[..., 4], target_confidence_noobj, reduction="sum")

        class_loss = F.binary_cross_entropy_with_logits(preds_obj[..., 5:], targets[..., 5:], reduction="sum")

        total_loss = class_loss + obj_loss + self.coord_coeff * coord_loss + self.no_object_coeff * noobj_loss

        return total_loss, coord_loss, obj_loss, noobj_loss, class_loss
