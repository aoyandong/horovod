# Copyright 2019 Uber Technologies, Inc. All Rights Reserved.
# Modifications copyright Microsoft
# Modifications copyright (C) 2020, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

# Load all the necessary PyTorch C types.
import torch

import warnings

from horovod.torch import mpi_lib_v2 as mpi_lib
from horovod.common.basics import HorovodBasics as _HorovodBasics
_NULL = ""
_basics = _HorovodBasics(__file__, 'mpi_lib_v2')

from horovod.common.exceptions import HorovodInternalError
from horovod.common.util import gpu_available, num_rank_is_power_2

from horovod.torch.compression import Compression

# import basic methods
init = _basics.init
is_initialized = _basics.is_initialized
start_timeline = _basics.start_timeline
stop_timeline = _basics.stop_timeline
size = _basics.size
local_size = _basics.local_size
rank = _basics.rank
local_rank = _basics.local_rank
mpi_threads_supported = _basics.mpi_threads_supported
mpi_enabled = _basics.mpi_enabled
mpi_built = _basics.mpi_built
gloo_enabled = _basics.gloo_enabled
gloo_built = _basics.gloo_built
nccl_built = _basics.nccl_built
ccl_built = _basics.ccl_built
cuda_built = _basics.cuda_built
rocm_built = _basics.rocm_built
def shutdown(*args, **kwargs):
    mpi_lib.horovod_torch_reset()
    return _basics.shutdown(*args, **kwargs)

# import reduction op values
Average = _basics.Average
Sum = _basics.Sum
Adasum = _basics.Adasum

is_homogeneous = _basics.is_homogeneous


# Schema: handle -> input, output
# We keep input in order to make sure it does not get garbage collected
# before the operation is finished.
_handle_map = {}


def _check_function(function_factory, tensor):
    function = function_factory(tensor)
    if not hasattr(mpi_lib, function):
        raise ValueError('Tensor type %s is not supported.' % tensor.type())
    if not tensor.is_contiguous():
        raise ValueError('Tensor is required to be contiguous.')
    return function


def _allreduce_function_factory(tensor):
    return 'horovod_torch_allreduce_async_' + tensor.type().replace('.', '_')


def _allreduce_async(tensor, output, name, op, prescale_factor, postscale_factor):
    # Set the divisor for reduced gradients to average when necessary
    if op == Average:
        if rocm_built():
            # For ROCm, perform averaging at framework level
            divisor = size()
            op = Sum
        else:
            divisor = 1

    elif op == Adasum:
        if tensor.device.type != 'cpu' and gpu_available('torch'):
            if nccl_built():
                if not is_homogeneous():
                    raise NotImplementedError('Running GPU Adasum on heterogeneous cluster is not supported yet.')
                elif not num_rank_is_power_2(int(size() / local_size())):
                    raise NotImplementedError('Running GPU Adasum with non-power of 2 nodes is not supported yet.')
                if rocm_built():
                    # For ROCm, perform averaging at framework level
                    divisor = local_size()
                else:
                    divisor = 1
            else:
                warnings.warn('Adasum reduction does not currently support GPU reduction using MPI. Tensors are '
                              'copied to CPU memory instead. To use Adasum for GPU reduction, please compile Horovod '
                              'with HOROVOD_GPU_OPERATIONS=NCCL.')
                divisor = 1
        else:
            if not num_rank_is_power_2(size()):
                raise NotImplementedError('Running Adasum with non-power of 2 ranks is not supported yet.')
            divisor = 1
    else:
        divisor = 1

    function = _check_function(_allreduce_function_factory, tensor)
    try:
        handle = getattr(mpi_lib, function)(tensor, output, divisor,
                                            name.encode() if name is not None else _NULL, op,
                                            prescale_factor, postscale_factor)
    except RuntimeError as e:
        raise HorovodInternalError(e)
    _handle_map[handle] = (tensor, output)
    return handle


def allreduce_async(tensor, name=None, op=Average,
                    prescale_factor=1.0, postscale_factor=1.0):
    """
    A function that performs asynchronous averaging or summation of the input tensor
    over all the Horovod processes. The input tensor is not modified.

    The reduction operation is keyed by the name. If name is not provided, an incremented
    auto-generated name is used. The tensor type and shape must be the same on all
    Horovod processes for a given name. The reduction will not start until all processes
    are ready to send and receive the tensor.

    Arguments:
        tensor: A tensor to reduce.
        name: A name of the reduction operation.
        op: The reduction operation to combine tensors across different
                   ranks. Defaults to Average if None is given.
        prescale_factor: Multiplicative factor to scale tensor before allreduce.
        postscale_factor: Multiplicative factor to scale tensor after allreduce.

    Returns:
        A handle to the allreduce operation that can be used with `poll()` or
        `synchronize()`.
    """
    output = tensor.new(tensor.shape)
    return _allreduce_async(tensor, output, name, op, prescale_factor, postscale_factor)


class HorovodAllreduce(torch.autograd.Function):
    """An autograd function that performs allreduce on a tensor."""

    @staticmethod
    def forward(ctx, tensor, name, op, prescale_factor, postscale_factor):
        ctx.op = op
        ctx.prescale_factor = prescale_factor
        ctx.postscale_factor = postscale_factor
        handle = allreduce_async(tensor, name, op, prescale_factor, postscale_factor)
        return synchronize(handle)

    @staticmethod
    def backward(ctx, grad_output):
        return allreduce(grad_output,
                         op=ctx.op,
                         prescale_factor=ctx.prescale_factor,
                         postscale_factor=ctx.postscale_factor), None, None, None, None, None


def allreduce(tensor, name=None, compression=Compression.none, op=Average,
              prescale_factor=1.0, postscale_factor=1.0):
    """
    A function that performs averaging or summation of the input tensor over all the
    Horovod processes. The input tensor is not modified.

    The reduction operation is keyed by the name. If name is not provided, an incremented
    auto-generated name is used. The tensor type and shape must be the same on all
    Horovod processes for a given name. The reduction will not start until all processes
    are ready to send and receive the tensor.

    This acts as a thin wrapper around an autograd function.  If your input
    tensor requires gradients, then callings this function will allow gradients
    to be computed and backpropagated.

    Arguments:
        tensor: A tensor to reduce.
        name: A name of the reduction operation.
        compression: Compression algorithm used during allreduce to reduce the amount
                     of data sent during the each parameter update step.  Defaults to
                     not using compression.
        op: The reduction operation to combine tensors across different ranks. Defaults
            to Average if None is given.
        prescale_factor: Multiplicative factor to scale tensor before allreduce.
        postscale_factor: Multiplicative factor to scale tensor after allreduce.

    Returns:
        A tensor of the same shape and type as `tensor`, averaged or summed across all
        processes.
    """
    tensor_compressed, ctx = compression.compress(tensor)
    summed_tensor_compressed = HorovodAllreduce.apply(tensor_compressed, name, op,
                                                      prescale_factor, postscale_factor)
    return compression.decompress(summed_tensor_compressed, ctx)


def allreduce_async_(tensor, name=None, op=Average,
                     prescale_factor=1.0, postscale_factor=1.0):
    """
    A function that performs asynchronous in-place averaging or summation of the input
    tensor over all the Horovod processes.

    The reduction operation is keyed by the name. If name is not provided, an incremented
    auto-generated name is used. The tensor type and shape must be the same on all
    Horovod processes for a given name. The reduction will not start until all processes
    are ready to send and receive the tensor.

    Arguments:
        tensor: A tensor to reduce.
        name: A name of the reduction operation.
        op: The reduction operation to combine tensors across different ranks. Defaults to
            Average if None is given.
        prescale_factor: Multiplicative factor to scale tensor before allreduce.
        postscale_factor: Multiplicative factor to scale tensor after allreduce.

    Returns:
        A handle to the allreduce operation that can be used with `poll()` or
        `synchronize()`.
    """
    return _allreduce_async(tensor, tensor, name, op, prescale_factor, postscale_factor)


def allreduce_(tensor, name=None, op=Average,
               prescale_factor=1.0, postscale_factor=1.0):
    """
    A function that performs in-place averaging or summation of the input tensor over
    all the Horovod processes.

    The reduction operation is keyed by the name. If name is not provided, an incremented
    auto-generated name is used. The tensor type and shape must be the same on all
    Horovod processes for a given name. The reduction will not start until all processes
    are ready to send and receive the tensor.

    Arguments:
        tensor: A tensor to reduce.
        name: A name of the reduction operation.
        op: The reduction operation to combine tensors across different ranks. Defaults to
            Average if None is given.
        prescale_factor: Multiplicative factor to scale tensor before allreduce.
        postscale_factor: Multiplicative factor to scale tensor after allreduce.

    Returns:
        A tensor of the same shape and type as `tensor`, averaged or summed across all
        processes.
    """
    handle = allreduce_async_(tensor, name, op, prescale_factor, postscale_factor)
    return synchronize(handle)


def _grouped_allreduce_function_factory(tensor):
    return 'horovod_torch_grouped_allreduce_async_' + tensor.type().replace('.', '_')


def _grouped_allreduce_async(tensors, outputs, name, op, prescale_factor, postscale_factor):
    # Set the divisor for reduced gradients to average when necessary
    if op == Average:
        if rocm_built():
            # For ROCm, perform averaging at framework level
            divisor = size()
            op = Sum
        else:
            divisor = 1
    elif op == Adasum:
        if tensors[0].device.type != 'cpu' and gpu_available('torch'):
            if nccl_built():
                if not is_homogeneous():
                    raise NotImplementedError('Running GPU Adasum on heterogeneous cluster is not supported yet.')
                elif not num_rank_is_power_2(int(size() / local_size())):
                    raise NotImplementedError('Running GPU Adasum with non-power of 2 nodes is not supported yet.')
                if rocm_built():
                    # For ROCm, perform averaging at framework level
                    divisor = local_size()
                else:
                    divisor = 1
            else:
                warnings.warn('Adasum reduction does not currently support GPU reduction using MPI. Tensors are '
                              'copied to CPU memory instead. To use Adasum for GPU reduction, please compile Horovod '
                              'with HOROVOD_GPU_OPERATIONS=NCCL.')
                divisor = 1
        else:
            if not num_rank_is_power_2(size()):
                raise NotImplementedError('Running Adasum with non-power of 2 ranks is not supported yet.')
            divisor = 1
    else:
        divisor = 1

    function = _check_function(_grouped_allreduce_function_factory, tensors[0])
    try:
        handle = getattr(mpi_lib, function)(tensors, outputs, divisor,
                                            name.encode() if name is not None else _NULL, op,
                                            prescale_factor, postscale_factor)
    except RuntimeError as e:
        raise HorovodInternalError(e)
    _handle_map[handle] = (tuple(tensors), tuple(outputs))
    return handle


def grouped_allreduce_async(tensors, name=None, op=Average,
                            prescale_factor=1.0, postscale_factor=1.0):
    """
    A function that performs asynchronous averaging or summation of the input tensor
    list over all the Horovod processes. The input tensors are not modified.

    The reduction operations are keyed by the base name. If a base name is not
    provided, an incremented auto-generated base name is used. Reductions are
    performed across tensors in the same list position. The tensor type and
    shape must be the same on all Horovod processes for tensors sharing
    positions in the input tensor list. The reduction will not start until all
    processes are ready to send and receive the tensors.

    Arguments:
        tensors: A list of tensors to reduce.
        name: A base name to use for the group reduction operation.
        op: The reduction operation to combine tensors across different
                   ranks. Defaults to Average if None is given.
        prescale_factor: Multiplicative factor to scale tensor before allreduce.
        postscale_factor: Multiplicative factor to scale tensor after allreduce.

    Returns:
        A handle to the group allreduce operation that can be used with `poll()` or
        `synchronize()`.
    """
    outputs = [t.new(t.shape) for t in tensors]
    return _grouped_allreduce_async(tensors, outputs, name, op, prescale_factor, postscale_factor)


class HorovodGroupedAllreduce(torch.autograd.Function):
    """An autograd function that performs allreduce on a list of tensors."""

    @staticmethod
    def forward(ctx, name, op, prescale_factor, postscale_factor, *tensors):
        ctx.op = op
        ctx.prescale_factor = prescale_factor
        ctx.postscale_factor = postscale_factor
        handle = grouped_allreduce_async(list(tensors), name, op, prescale_factor, postscale_factor)
        return synchronize(handle)

    @staticmethod
    def backward(ctx, *grad_output):
        grad_reduced = grouped_allreduce(list(grad_output),
                                         op=ctx.op,
                                         prescale_factor=ctx.prescale_factor,
                                         postscale_factor=ctx.postscale_factor)
        return (None, None, None, None, None, *grad_reduced)


def grouped_allreduce(tensors, name=None, compression=Compression.none, op=Average,
                      prescale_factor=1.0, postscale_factor=1.0):
    """
    A function that performs averaging or summation of the input tensor
    list over all the Horovod processes. The input tensors are not modified.

    The reduction operations are keyed by the base name. If a base name is not
    provided, an incremented auto-generated base name is used. Reductions are
    performed across tensors in the same list position. The tensor type and
    shape must be the same on all Horovod processes for tensors sharing
    positions in the input tensor list. The reduction will not start until all
    processes are ready to send and receive the tensors.

    This acts as a thin wrapper around an autograd function.  If your input
    tensors require gradients, then calling this function will allow gradients
    to be computed and backpropagated.

    Arguments:
        tensors: A list of tensors to reduce.
        name: A base name to use for the group reduction operation.
        compression: Compression algorithm used during allreduce to reduce the amount
                     of data sent during the each parameter update step.  Defaults to
                     not using compression.
        op: The reduction operation to combine tensors across different ranks. Defaults
            to Average if None is given.
        prescale_factor: Multiplicative factor to scale tensor before allreduce.
        postscale_factor: Multiplicative factor to scale tensor after allreduce.

    Returns:
        A list containing tensors of the same shape and type as in `tensors`,
        averaged or summed across all processes.
    """
    tensors_compressed, ctxs = zip(*[compression.compress(t) for t in tensors])
    summed_tensors_compressed = HorovodGroupedAllreduce.apply(name, op,
                                                              prescale_factor, postscale_factor,
                                                              *tensors_compressed)
    return [compression.decompress(t, ctx) for t, ctx in zip(summed_tensors_compressed, ctxs)]


def grouped_allreduce_async_(tensors, name=None, op=Average,
                             prescale_factor=1.0, postscale_factor=1.0):
    """
    A function that performs asynchronous in-place averaging or summation of the input
    tensors over all the Horovod processes.

    The reduction operations are keyed by the base name. If a base name is not
    provided, an incremented auto-generated base name is used. Reductions are
    performed across tensors in the same list position. The tensor type and
    shape must be the same on all Horovod processes for tensors sharing
    positions in the input tensor list. The reduction will not start until all
    processes are ready to send and receive the tensors.

    Arguments:
        tensors: A list of tensors to reduce.
        name: A base name to use for the group reduction operation.
        op: The reduction operation to combine tensors across different ranks. Defaults to
            Average if None is given.
        prescale_factor: Multiplicative factor to scale tensor before allreduce.
        postscale_factor: Multiplicative factor to scale tensor after allreduce.

    Returns:
        A handle to the group allreduce operation that can be used with `poll()` or
        `synchronize()`.
    """
    return _grouped_allreduce_async(tensors, tensors, name, op, prescale_factor, postscale_factor)


def grouped_allreduce_(tensors, name=None, op=Average,
                       prescale_factor=1.0, postscale_factor=1.0):
    """
    A function that performs in-place averaging or summation of the input tensors over
    all the Horovod processes.

    The reduction operations are keyed by the base name. If a base name is not
    provided, an incremented auto-generated base name is used. Reductions are
    performed across tensors in the same list position. The tensor type and
    shape must be the same on all Horovod processes for tensors sharing
    positions in the input tensor list. The reduction will not start until all
    processes are ready to send and receive the tensors.

    Arguments:
        tensors: A list of tensors to reduce.
        name: A base name to use for the group reduction operation.
        op: The reduction operation to combine tensors across different ranks. Defaults to
            Average if None is given.
        prescale_factor: Multiplicative factor to scale tensor before allreduce.
        postscale_factor: Multiplicative factor to scale tensor after allreduce.

    Returns:
        A list containing tensors of the same shape and type as in `tensors`,
        averaged or summed across all processes.
    """
    handle = grouped_allreduce_async_(tensors, name, op, prescale_factor, postscale_factor)
    return synchronize(handle)


def _allgather_function_factory(tensor):
    return 'horovod_torch_allgather_async_' + tensor.type().replace('.', '_')


def _allgather_async(tensor, output, name):
    function = _check_function(_allgather_function_factory, tensor)
    try:
        handle = getattr(mpi_lib, function)(
            tensor, output, name.encode() if name is not None else _NULL)
    except RuntimeError as e:
        raise HorovodInternalError(e)
    _handle_map[handle] = (tensor, output)
    return handle


def allgather_async(tensor, name=None):
    """
    A function that asynchronously concatenates the input tensor with the same input
    tensor on all other Horovod processes. The input tensor is not modified.

    The concatenation is done on the first dimension, so the input tensors on the
    different processes must have the same rank and shape, except for the first
    dimension, which is allowed to be different.

    Arguments:
        tensor: A tensor to allgather.
        name: A name of the allgather operation.

    Returns:
        A handle to the allgather operation that can be used with `poll()` or
        `synchronize()`.
    """
    output = tensor.new()
    return _allgather_async(tensor, output, name)


class HorovodAllgather(torch.autograd.Function):
    """An autograd function that performs allgather on a tensor."""

    @staticmethod
    def forward(ctx, tensor, name):
        ctx.dim = tensor.shape[0]
        handle = allgather_async(tensor, name)
        return synchronize(handle)

    @staticmethod
    def backward(ctx, grad_output):
        grad_reduced = allreduce(grad_output, op=Sum)

        dim_t = torch.IntTensor([ctx.dim])
        dim = allgather(dim_t).view(size())

        r = rank()
        offset = torch.sum(dim.narrow(0, 0, r)).item() if r != 0 else 0
        return grad_reduced.narrow(0, offset, ctx.dim), None


def allgather(tensor, name=None):
    """
    A function that concatenates the input tensor with the same input tensor on
    all other Horovod processes. The input tensor is not modified.

    The concatenation is done on the first dimension, so the input tensors on the
    different processes must have the same rank and shape, except for the first
    dimension, which is allowed to be different.

    This acts as a thin wrapper around an autograd function.  If your input
    tensor requires gradients, then callings this function will allow gradients
    to be computed and backpropagated.

    Arguments:
        tensor: A tensor to allgather.
        name: A name of the allgather operation.

    Returns:
        A tensor of the same type as `tensor`, concatenated on dimension zero
        across all processes. The shape is identical to the input shape, except for
        the first dimension, which may be greater and is the sum of all first
        dimensions of the tensors in different Horovod processes.
    """
    return HorovodAllgather.apply(tensor, name)


def _broadcast_function_factory(tensor):
    return 'horovod_torch_broadcast_async_' + tensor.type().replace('.', '_')


def _broadcast_async(tensor, output, root_rank, name):
    function = _check_function(_broadcast_function_factory, tensor)
    try:
        handle = getattr(mpi_lib, function)(
            tensor, output, root_rank, name.encode() if name is not None else _NULL)
    except RuntimeError as e:
        raise HorovodInternalError(e)
    _handle_map[handle] = (tensor, output)
    return handle


def broadcast_async(tensor, root_rank, name=None):
    """
    A function that asynchronously broadcasts the input tensor on root rank to the same
    input tensor on all other Horovod processes. The input tensor is not modified.

    The broadcast operation is keyed by the name. If name is not provided, an incremented
    auto-generated name is used. The tensor type and shape must be the same on all
    Horovod processes for a given name. The broadcast will not start until all processes
    are ready to send and receive the tensor.

    Arguments:
        tensor: A tensor to broadcast.
        root_rank: The rank to broadcast the value from.
        name: A name of the broadcast operation.

    Returns:
        A handle to the broadcast operation that can be used with `poll()` or
        `synchronize()`.
    """
    output = tensor.new(tensor.shape)
    return _broadcast_async(tensor, output, root_rank, name)


class HorovodBroadcast(torch.autograd.Function):
    """An autograd function that broadcasts a tensor."""

    @staticmethod
    def forward(ctx, tensor, root_rank, name):
        ctx.root_rank = root_rank
        handle = broadcast_async(tensor, root_rank, name)
        return synchronize(handle)

    @staticmethod
    def backward(ctx, grad_output):
        grad_reduced = allreduce(grad_output, op=Sum)
        if rank() != ctx.root_rank:
            grad_reduced *= 0
        return grad_reduced, None, None


def broadcast(tensor, root_rank, name=None):
    """
    A function that broadcasts the input tensor on root rank to the same input tensor
    on all other Horovod processes. The input tensor is not modified.

    The broadcast operation is keyed by the name. If name is not provided, an incremented
    auto-generated name is used. The tensor type and shape must be the same on all
    Horovod processes for a given name. The broadcast will not start until all processes
    are ready to send and receive the tensor.

    This acts as a thin wrapper around an autograd function.  If your input
    tensor requires gradients, then callings this function will allow gradients
    to be computed and backpropagated.

    Arguments:
        tensor: A tensor to broadcast.
        root_rank: The rank to broadcast the value from.
        name: A name of the broadcast operation.

    Returns:
        A tensor of the same shape and type as `tensor`, with the value broadcasted
        from root rank.
    """
    return HorovodBroadcast.apply(tensor, root_rank, name)


def broadcast_async_(tensor, root_rank, name=None):
    """
    A function that asynchronously broadcasts the input tensor on root rank to the same
    input tensor on all other Horovod processes. The operation is performed in-place.

    The broadcast operation is keyed by the name. If name is not provided, an incremented
    auto-generated name is used. The tensor type and shape must be the same on all
    Horovod processes for a given name. The broadcast will not start until all processes
    are ready to send and receive the tensor.

    Arguments:
        tensor: A tensor to broadcast.
        root_rank: The rank to broadcast the value from.
        name: A name of the broadcast operation.

    Returns:
        A handle to the broadcast operation that can be used with `poll()` or
        `synchronize()`.
    """
    return _broadcast_async(tensor, tensor, root_rank, name)


def broadcast_(tensor, root_rank, name=None):
    """
    A function that broadcasts the input tensor on root rank to the same input tensor
    on all other Horovod processes. The operation is performed in-place.

    The broadcast operation is keyed by the name. If name is not provided, an incremented
    auto-generated name is used. The tensor type and shape must be the same on all
    Horovod processes for a given name. The broadcast will not start until all processes
    are ready to send and receive the tensor.

    Arguments:
        tensor: A tensor to broadcast.
        root_rank: The rank to broadcast the value from.
        name: A name of the broadcast operation.

    Returns:
        A tensor of the same shape and type as `tensor`, with the value broadcasted
        from root rank.
    """
    handle = broadcast_async_(tensor, root_rank, name)
    return synchronize(handle)

def _alltoall_function_factory(tensor):
    return 'horovod_torch_alltoall_async_' + tensor.type().replace('.', '_')

def _alltoall_async(tensor, splits, output, name):
    if splits is None:
        # If splits not provided, create empty tensor as placeholder
        splits = torch.tensor([], dtype=torch.int32, device='cpu')
    elif not isinstance(splits, torch.Tensor):
        splits = torch.tensor(splits, dtype=torch.int32, device='cpu')

    function = _check_function(_alltoall_function_factory, tensor)
    try:
        handle = getattr(mpi_lib, function)(
            tensor, splits, output, name.encode() if name is not None else _NULL)
    except RuntimeError as e:
        raise HorovodInternalError(e)
    _handle_map[handle] = (tensor, splits, output)
    return handle


def alltoall_async(tensor, splits=None, name=None):
    """
    A function that scatters slices of the input tensor to all other Horovod processes
    and returns a tensor of gathered slices from all other Horovod processes. The input
    tensor is not modified.

    The slicing is done on the first dimension, so the input tensors on
    the different processes must have the same rank and shape, except for the
    first dimension, which is allowed to be different.

    Arguments:
        tensor: A tensor to distribute with alltoall.
        splits: A tensor of integers in rank order describing how many
                elements in `tensor` to send to each worker.  Splitting is
                applied along the first dimension of `tensor`. If `splits` is
                not provided, the first dimension is split equally by the
                number of Horovod processes.
        name: A name of the alltoall operation.

    Returns:
        A handle to the alltoall operation that can be used with `poll()` or
        `synchronize()`.
    """
    output = tensor.new()
    return _alltoall_async(tensor, splits, output, name)


class HorovodAlltoall(torch.autograd.Function):
    """An autograd function that performs alltoall on a tensor."""

    @staticmethod
    def forward(ctx, tensor, splits, name):
        ctx.tensor = tensor
        ctx.splits = splits
        handle = alltoall_async(tensor, splits, name)
        return synchronize(handle)

    @staticmethod
    def backward(ctx, grad_output):
        recvsplits = None
        if ctx.splits is not None:
            recvsplits = alltoall(ctx.splits, splits=torch.ones(size(), dtype=torch.int32, device='cpu'))
        else:
            splits_equal = torch.ones(size(), dtype=torch.int32, device='cpu') * (ctx.tensor.size()[0] // size())
            recvsplits = alltoall(splits_equal, splits=torch.ones(size(), dtype=torch.int32, device='cpu'))
        return alltoall(grad_output, splits=recvsplits), None, None


def alltoall(tensor, splits=None, name=None):
    """
    A function that scatters slices of the input tensor to all other Horovod processes
    and returns a tensor of gathered slices from all other Horovod processes. The input
    tensor is not modified.

    The slicing is done on the first dimension, so the input tensors on
    the different processes must have the same rank and shape, except for the
    first dimension, which is allowed to be different.

    This acts as a thin wrapper around an autograd function.  If your input
    tensor requires gradients, then callings this function will allow gradients
    to be computed and backpropagated.

    Arguments:
        tensor: A tensor to distribute with alltoall.
        splits: A tensor of integers in rank order describing how many
                elements in `tensor` to send to each worker.  Splitting is
                applied along the first dimension of `tensor`. If `splits` is
                not provided, the first dimension is split equally by the
                number of Horovod processes.
        name: A name of the alltoall operation.

    Returns:
        A tensor containing the gathered tensor data from all workers.
    """
    return HorovodAlltoall.apply(tensor, splits, name)


def poll(handle):
    """
    Polls an allreduce, allgather or broadcast handle to determine whether underlying
    asynchronous operation has completed. After `poll()` returns `True`, `synchronize()`
    will return without blocking.

    Arguments:
        handle: A handle returned by an allreduce, allgather or broadcast asynchronous
                operation.

    Returns:
        A flag indicating whether the operation has completed.
    """
    return mpi_lib.horovod_torch_poll(handle) != 0


def synchronize(handle):
    """
    Synchronizes an asynchronous allreduce, allgather or broadcast operation until
    it's completed. Returns the result of the operation.

    Arguments:
        handle: A handle returned by an allreduce, allgather or broadcast asynchronous
                operation.

    Returns:
        An output tensor of the operation.
    """
    if handle not in _handle_map:
        return

    try:
        mpi_lib.horovod_torch_wait_and_clear(handle)
        output = _handle_map.pop(handle)[-1]
        return output
    except RuntimeError as e:
        raise HorovodInternalError(e)


def join(device=-1):
    """A function that indicates that the rank finished processing data.

    All ranks that did not call join() continue to process allreduce operations.
    This function blocks Python thread until all ranks join.

    Arguments:
        device: An id of the device to create temprorary zero tensors (default -1, CPU)

    Returns:
        Id of the rank that joined last.
    """
    try:
        return mpi_lib.horovod_torch_join(device)
    except RuntimeError as e:
        raise HorovodInternalError(e)
