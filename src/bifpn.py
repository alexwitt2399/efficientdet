from typing import List, Dict
import collections

import dataclasses
import torch
import numpy as np

from src import efficientnet


@dataclasses.dataclass
class node_param:
    feat_level: int
    offsets: List[int]
    upsample: bool


_NODE_PARAMS = [
    node_param(6, [3, 4], True),
    node_param(5, [2, 5], True),
    node_param(4, [1, 6], True),
    node_param(3, [0, 7], True),
    node_param(4, [1, 7, 8], False),
    node_param(5, [2, 6, 9], False),
    node_param(6, [3, 5, 10], False),
    node_param(7, [4, 11], False),
]


def depthwise(in_channels: int, out_channels: int):
    """ A depthwise separable linear layer. """
    return [
        torch.nn.Conv2d(
            in_channels,
            in_channels,
            kernel_size=3,
            padding=1,
            bias=False,
            groups=in_channels,
        ),
        torch.nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=True),
    ]


def conv3x3(in_channels: int, out_channels: int):
    """ Simple Conv2d layer. """
    return [
        torch.nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1, bias=True),
    ]


class BiFPN(torch.nn.Module):
    """ Implementation of thee BiFPN originally proposed in 
    https://arxiv.org/pdf/1911.09070.pdf. """

    def __init__(
        self,
        in_channels: List[int],
        out_channels: int,
        num_bifpns: int,
        bifpn_height: int = 5,
        levels: List[int] = [3, 4, 5],
    ) -> None:
        """ 
        Args:
            in_channels: A list of the incomming number of filters for each pyramid 
                level.
            out_channels: The number of features used within the BiFPN. 
            num_bifpns: The number of BiFPN layers in the model. 
            bifpn_height: The number of feature maps to send in to the
                bifpn. NOTE might not be equal to num_levels_in. 
            levels: The idxs of the levels coming in to the BiFPN. 
        """
        super().__init__()
        self.levels_in = levels
        self.bifpn_height = bifpn_height
        self.in_channels = in_channels

        # If BiFPN needs more levels than what is being put in, downsample the incoming
        # level to form lower resolution levels.
        if self.bifpn_height != len(self.levels_in):

            # Before the first BiFPN level, we need to dowsample the incoming most
            # 'low-resolution' level to create the necessary number of levels. Only
            # before the first downsample is a pointwise conv applied to fix the
            # channel depth to the number of channels in the BiFPN.
            self.downsample_convs = [
                torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels[-1], out_channels, kernel_size=1),
                    torch.nn.MaxPool2d(kernel_size=3, padding=1, stride=2),
                )
            ] + [
                torch.nn.MaxPool2d(kernel_size=3, padding=1, stride=2)
                for _ in range(self.bifpn_height - len(levels) - 1)
            ]
            self.downsample_convs = torch.nn.Sequential(*self.downsample_convs)

        # Specify the channels for the first bifpn layer. It will be the top most
        # 'low-res' feature maps plus however many extra levels are made above (typically
        # two).
        level_channels = in_channels[-len(levels) :] + [out_channels] * (
            self.bifpn_height - len(levels)
        )

        # Construct the BiFPN layers. We need to keep track of the channels of the inputs
        # to each level. The first level will have channel depths _not equal_ to the
        # internal feature depth. The original input feature maps will be squashed to the
        # proper channel depth _twice_.
        channel_dict = {level: level_channels[idx] for idx, level in enumerate(levels)}
        self.bifp_layers = torch.nn.Sequential()
        for idx in range(num_bifpns):
            self.bifp_layers.add_module(
                f"BiFPN_{idx}",
                BiFPNBlock(
                    channels=out_channels,
                    num_levels=bifpn_height,
                    channels_in={idx: out_channels for idx in levels}
                    if idx
                    else channel_dict,
                ),
            )

    def __call__(self, feature_maps: collections.OrderedDict) -> List[torch.Tensor]:
        """ First apply the lateral convolutions to size all the incoming 
        feature layers to the same size. Then pass through the BiFPN blocks.

        Args:
            feature_maps: Feature maps in sorted order of layer. 
        """

        # Apply the downsampling to form the top layers.
        for layer in self.downsample_convs:

            # Get the top most layer which is the last in the dict.
            top_level_idx, top_level_map = next(reversed(feature_maps.items()))
            feature_maps[top_level_idx + 1] = layer(top_level_map)

        return self.bifp_layers(feature_maps)


class BiFPNBlock(torch.nn.Module):
    """ Modular implementation of a single BiFPN layer. """

    def __init__(
        self, channels: int, num_levels: int, channels_in: Dict[int, int]
    ) -> None:
        """
        Args:
            channels: The number of channels in and out.
            num_levels: The number incoming feature pyramid levels.
            channels_in: The channels for each input level to the block. This is really
                important for the first level which must adapt the channel depth of the
                original feature levels twice.
        """
        super().__init__()
        self.num_levels = num_levels
        self.combines = torch.nn.Sequential()
        self.post_combines = torch.nn.Sequential()
        self.index_offset = num_levels - len(channels_in) + 1

        # Create node combination and depthwise separable convolutions that will process
        # the input feature maps.
        for idx, node in enumerate(_NODE_PARAMS):

            # Combine the nodes first.
            self.combines.add_module(
                f"combine_{node.offsets}",
                CombineLevels(node, self.index_offset, channels, channels_in),
            )

            # Apply output convolution.
            self.post_combines.add_module(
                f"post_combine_{node.offsets}",
                torch.nn.Sequential(
                    torch.nn.Conv2d(
                        in_channels=channels,
                        out_channels=channels,
                        kernel_size=3,
                        padding=1,
                        groups=channels,
                        bias=False,
                    ),
                    torch.nn.Conv2d(
                        in_channels=channels,
                        out_channels=channels,
                        kernel_size=1,
                        bias=True,
                    ),
                    torch.nn.BatchNorm2d(channels, momentum=0.01, eps=1e-3),
                    efficientnet.Swish(),
                ),
            )

    def __call__(self, input_maps: collections.OrderedDict) -> collections.OrderedDict:
        """ NOTE: One might find it useful to observe the orignal paper's
        diagram while reading this code. 

        Args:
            feature_maps: A list of the feature maps from each of the
                pyramid levels. Highest to lowest.
        """
        level_id_offset = next(iter(input_maps.keys()))
        for combine, post_combine_conv in zip(self.combines, self.post_combines):
            level_idx = next(reversed(input_maps.keys()))
            input_maps[level_idx + 1] = post_combine_conv(combine(input_maps))

        # Only return the last self.num_levels levels.
        retval = collections.OrderedDict()
        for idx, level in enumerate(input_maps.values()):
            if idx < len(input_maps) - self.num_levels:
                continue

            retval[len(retval) + level_id_offset] = level
    
        return retval


class CombineLevels(torch.nn.Module):
    def __init__(
        self,
        param: node_param,
        index_offset: int,
        channels: int,
        levels_in: Dict[int, int] = {},
    ) -> None:
        """ Args:
            input_offsets: The node ids to combine.
        """
        super().__init__()
        self.eps = 1e-4
        self.upsample = param.upsample
        self.offsets = [index_offset + offset for offset in param.offsets]
        self.levels_in = levels_in

        # Construct lateral convolutions if any of the original input levels
        # are part of this node. The lateral convs are needed to homogenize
        # the channel depth.
        self.lateral_node = None
        for offset in self.offsets:
            if offset in levels_in and levels_in[offset] != channels:
                self.lateral_node = offset
                self.lateral_conv = torch.nn.Conv2d(levels_in[offset], channels, kernel_size=1)

        # Construct the resample module.
        if param.upsample:
            # If upsample, use interpolation.
            self.resample = torch.nn.UpsamplingNearest2d(scale_factor=2)
        else:
            # If downsample, use pooling.
            self.resample = torch.nn.Sequential(
                torch.nn.MaxPool2d(kernel_size=3, padding=1, stride=2),
            )

        # Right now only the fast attention addition method is supported.
        self.weights = torch.nn.Parameter(
            torch.ones([len(self.offsets)]), requires_grad=True
        )

    def __call__(self, x: collections.OrderedDict) -> collections.OrderedDict:

        # Extract the nodes this combination module considers.
        nodes = collections.OrderedDict()
        for node in x:
            # Apply lateral convs if needed. This is only needed on the first sublayer
            # of the first bifpn block due to the size of the original pyramid levels
            # extracted from the backbone.
            if node == self.lateral_node:
                nodes[node] = self.lateral_conv(x[node])
            elif node in self.offsets and node != max(self.offsets):
                nodes[node] = x[node]

        nodes[max(self.offsets)] = self.resample(x[max(self.offsets)])

        # Now combine all the nodes.
        weights = torch.nn.functional.relu(self.weights)
        new_node = torch.stack(
            [
                nodes[offset] * weights[idx] / torch.sum(weights + self.eps)
                for idx, offset in enumerate(self.offsets)
            ],
            dim=-1,
        )

        return torch.sum(new_node, dim=-1)
