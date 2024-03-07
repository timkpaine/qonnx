# Copyright (c) 2024, Advanced Micro Devices, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of QONNX nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import pytest

import numpy as np
import onnx
import onnx.helper as oh
import onnx.numpy_helper as nph
from onnx import TensorProto
from onnx.checker import check_model
from pkgutil import get_data

import qonnx.core.onnx_exec as oxe
from qonnx.core.datatype import DataType
from qonnx.core.modelwrapper import ModelWrapper
from qonnx.transformation.infer_shapes import InferShapes
from qonnx.transformation.resize_conv_to_deconv import ResizeConvolutionToDeconvolution
from qonnx.util.basic import gen_finn_dt_tensor, qonnx_make_model

np.random.seed(0)


@pytest.mark.parametrize("maintain_bit_width", [True, False])
def test_resize_conv_to_deconv_float_model(maintain_bit_width: bool):
    raw_m = get_data("qonnx.data", "onnx/bsd300x3-espcn/nn_resize/float_model.onnx")
    model = ModelWrapper(raw_m)
    model = model.transform(InferShapes())
    iname = model.graph.input[0].name
    oname = model.graph.output[0].name
    ishape = model.get_tensor_shape(iname)
    rand_inp = gen_finn_dt_tensor(DataType["FLOAT32"], ishape)
    input_dict = {iname: rand_inp}
    expected = oxe.execute_onnx(model, input_dict)[oname]
    new_model = model.transform(ResizeConvolutionToDeconvolution(maintain_bit_width=maintain_bit_width))
    # check that there are no Resize ops left
    op_types = list(map(lambda x: x.op_type, new_model.graph.node))
    assert "Resize" not in op_types, "Error: the Resize nodes should be removed."
    produced = oxe.execute_onnx(new_model, input_dict)[oname]
    assert np.isclose(expected, produced, atol=1e-4).all(), "Error: expected output does not match the produced output."


@pytest.mark.parametrize("maintain_bit_width", [True, False])
def test_resize_conv_to_deconv_quant_model(maintain_bit_width: bool):
    # get raw quantized model with reference input
    raw_i = get_data("qonnx.data", "onnx/bsd300x3-espcn/test_data/input_0.pb")
    raw_m = get_data("qonnx.data", "onnx/bsd300x3-espcn/nn_resize/quant_model.onnx")
    # create model from the onnx file and infer the shapes
    model = ModelWrapper(raw_m)
    model = model.transform(InferShapes())
    iname = model.graph.input[0].name
    oname = model.graph.output[0].name
    ishape = model.get_tensor_shape(iname)
    # load the reference input tensor
    input_tensor = onnx.load_tensor_from_string(raw_i)
    input_tensor = nph.to_array(input_tensor)
    assert list(input_tensor.shape) == ishape, "Error: reference input doesn't match loaded model."
    input_dict = {iname: input_tensor}
    # get the output from the sub-pixel convolution model
    output_resize_conv = oxe.execute_onnx(model, input_dict)[oname]
    # translate the sub-pixel convolution to the deconvolution
    new_model = model.transform(ResizeConvolutionToDeconvolution(maintain_bit_width=maintain_bit_width))
    # check that there are no Resize ops left
    op_types = list(map(lambda x: x.op_type, new_model.graph.node))
    assert "Resize" not in op_types, "Error: the Resize nodes should be removed."
    # get the output from the deconvolution model
    output_deconv = oxe.execute_onnx(new_model, input_dict)[oname]
    # maintaining the specified bit width introduces additional clipping errors that
    # shouldn't be expected to maintain reasonable functional similarity
    if not maintain_bit_width:
        assert np.isclose(
            output_deconv, output_resize_conv, atol=1 / 255.0, rtol=1.0
        ).all(), "Error: expected output does not match the produced output."


def create_nn_resize_conv_model(
    in_channels: int, out_channels: int, input_dim: int, kernel_size: int, upscale_factor: int, bias: bool
):
    assert isinstance(kernel_size, int), "Assuming square kernels, so kernel_size needs to be an int."
    padding = (kernel_size - 1) // 2

    ifm_ch = in_channels
    ifm_dim = input_dim
    ofm_dim = ifm_dim * upscale_factor
    ofm_ch = out_channels
    scales = np.array([1.0, 1.0, upscale_factor, upscale_factor], dtype=np.float32)

    resize = oh.make_node(
        "Resize",
        inputs=["inp", "roi", "scales"],
        outputs=["hid"],
        mode="nearest",
    )
    conv = oh.make_node(
        op_type="Conv",
        inputs=["hid", "W"] if not bias else ["hid", "W", "B"],
        outputs=["out"],
        kernel_shape=[kernel_size, kernel_size],
        pads=[padding, padding, padding, padding],
        strides=[1, 1],
        group=1,
        dilations=[1, 1],
    )

    input_shape = [1, ifm_ch, ifm_dim, ifm_dim]
    output_shape = [1, ofm_ch, ofm_dim, ofm_dim]

    conv_param_shape = [ofm_ch, ifm_ch, kernel_size, kernel_size]
    bias_param_shape = [ofm_ch]

    inp = oh.make_tensor_value_info("inp", TensorProto.FLOAT, input_shape)
    out = oh.make_tensor_value_info("out", TensorProto.FLOAT, output_shape)

    W_conv = oh.make_tensor_value_info("W", TensorProto.FLOAT, conv_param_shape)
    B_conv = oh.make_tensor_value_info("B", TensorProto.FLOAT, bias_param_shape)

    value_info = [W_conv] if not bias else [W_conv, B_conv]

    graph = oh.make_graph(
        nodes=[resize, conv],
        name="cnv_graph",
        inputs=[inp],
        outputs=[out],
        value_info=value_info,
    )
    modelproto = qonnx_make_model(graph, producer_name="test_model")
    model = ModelWrapper(modelproto)
    model.set_initializer("roi", np.empty(0))
    model.set_initializer("scales", scales)
    model.set_initializer("W", np.random.rand(*conv_param_shape).astype(np.float32))
    if bias:
        model.set_initializer("B", np.random.rand(*bias_param_shape).astype(np.float32))
    model = model.transform(InferShapes())
    check_model(model._model_proto)
    return model


@pytest.mark.parametrize("kernel_size", [1, 3, 5, 7])
@pytest.mark.parametrize("upscale_factor", [1, 2, 3, 4])
@pytest.mark.parametrize("bias", [True, False])
def test_resize_conv_to_deconv_layer(kernel_size: int, upscale_factor: int, bias: bool):
    # Create resize convolution layer that upsamples a 4x4 image with 1 I/O channel
    model_1 = create_nn_resize_conv_model(3, 10, 4, kernel_size, upscale_factor, bias)
    model_2 = model_1.transform(ResizeConvolutionToDeconvolution())
    input_shape = [1, 3, 4, 4]
    inp_dict = {"inp": np.random.rand(*input_shape).astype(np.float32)}
    assert oxe.compare_execution(model_1, model_2, inp_dict)
