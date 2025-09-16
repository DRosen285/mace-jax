import math
import warnings
from functools import reduce
from operator import mul
from typing import Any, Optional, Union

import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np
from e3nn import get_optimization_defaults
from e3nn_jax import Irreps

from ._codegen import codegen_tensor_product_left_right, codegen_tensor_product_right
from ._instruction import Instruction


def prod(iterable):
    return reduce(mul, iterable, 1)


def sqrt(x):
    return jnp.sqrt(x)


class TensorProduct(hk.Module):
    r"""Tensor product with parametrized paths (JAX/Haiku version).

    Parameters
    ----------
    irreps_in1 : `Irreps`
        Irreps for the first input.

    irreps_in2 : `Irreps`
        Irreps for the second input.

    irreps_out : `Irreps`
        Irreps for the output.

    instructions : List of tuple
        List of instructions ``(i_1, i_2, i_out, mode, train[, path_weight])``.

        Each instruction specifies that ``in1[i_1]`` :math:`\otimes` ``in2[i_2]`` contributes to ``out[i_out]``.

        * ``mode``: `str`. Determines how multiplicities are treated. `"uvw"` is fully connected.
        Other valid options: ``'uvw'``, ``'uvu'``, ``'uvv'``, ``'uuw'``, ``'uuu'``, ``'uvuv'``.
        * ``train``: `bool`. `True` if this path should have learnable weights, otherwise `False`.
        * ``path_weight``: `float`. Fixed multiplicative weight for the path. Defaults to 1. Overrides normalization from `in1_var`, `in2_var`, `out_var`.

    in1_var : list of float, array, or None
        Variance for each irrep in ``irreps_in1``. Defaults to 1.0 if `None`.

    in2_var : list of float, array, or None
        Variance for each irrep in ``irreps_in2``. Defaults to 1.0 if `None`.

    out_var : list of float, array, or None
        Variance for each irrep in ``irreps_out``. Defaults to 1.0 if `None`.

    irrep_normalization : {'component', 'norm', 'none'}, default 'component'
        Normalization of input and output representations:

        - `"component"`: each component is normalized.
        - `"norm"`: the tensor product preserves the norm.
        - `"none"`: no normalization applied.

    path_normalization : {'element', 'path', 'none'}, default 'element'
        Normalization applied to paths:

        - `"element"`: each output is normalized by the total number of elements independently of their paths.
        - `"path"`: each path is normalized by the number of elements in the path, then each output is normalized by the number of paths.
        - `"none"`: no normalization applied.

    internal_weights : bool
        Whether the module contains its learnable weights as internal Haiku parameters.

    shared_weights : bool
        Whether the learnable weights are shared across batch dimensions:

        - `True`: same weight used for all batch elements.
        - `False`: each batch element has its own weights (requires `internal_weights=False`).

    Examples
    --------
    Compute the cross product of vectors:

    >>> module = TensorProduct(
    ...     "16x1o", "16x1o", "16x1e",
    ...     [
    ...         (0, 0, 0, "uuu", False)
    ...     ]
    ... )

    Fully connected combination of vectors:

    >>> module = TensorProduct(
    ...     [(16, (1, -1))],
    ...     [(16, (1, -1))],
    ...     [(16, (1,  1))],
    ...     [
    ...         (0, 0, 0, "uvw", True)
    ...     ]
    ... )

    With custom path weights and input variances:

    >>> module = TensorProduct(
    ...     "8x0o + 8x1o",
    ...     "16x1o",
    ...     "16x1e",
    ...     [
    ...         (0, 0, 0, "uvw", True, 3),
    ...         (1, 0, 0, "uvw", True, 1),
    ...     ],
    ...     in2_var=[1/16]
    ... )

    Example of a dot product:

    >>> irreps = Irreps("3x0e + 4x0o + 1e + 2o + 3o")
    >>> module = TensorProduct(irreps, irreps, "0e", [
    ...     (i, i, 0, 'uuw', False)
    ...     for i, (mul, ir) in enumerate(irreps)
    ... ])

    Implement :math:`z_u = x_u \otimes (\sum_v w_{uv} y_v)`

    >>> module = TensorProduct(
    ...     "8x0o + 7x1o + 3x2e",
    ...     "10x0e + 10x1e + 10x2e",
    ...     "8x0o + 7x1o + 3x2e",
    ...     [
    ...         # paths for l=0
    ...         (0, 0, 0, "uvu", True),
    ...         # paths for l=1
    ...         (1, 0, 1, "uvu", True),
    ...         (1, 1, 1, "uvu", True),
    ...         (1, 2, 1, "uvu", True),
    ...         # paths for l=2
    ...         (2, 0, 2, "uvu", True),
    ...         (2, 1, 2, "uvu", True),
    ...         (2, 2, 2, "uvu", True),
    ...     ]
    ... )

    Tensor Product using Xavier uniform initialization:

    >>> irreps_1 = Irreps("5x0e + 10x1o + 1x2e")
    >>> irreps_2 = Irreps("5x0e + 10x1o + 1x2e")
    >>> irreps_out = Irreps("5x0e + 10x1o + 1x2e")
    >>> module = TensorProduct(
    ...     irreps_1,
    ...     irreps_2,
    ...     irreps_out,
    ...     [
    ...         (i_1, i_2, i_out, "uvw", True, mul_1 * mul_2)
    ...         for i_1, (mul_1, ir_1) in enumerate(irreps_1)
    ...         for i_2, (mul_2, ir_2) in enumerate(irreps_2)
    ...         for i_out, (mul_out, ir_out) in enumerate(irreps_out)
    ...         if ir_out in ir_1 * ir_2
    ...     ]
    ... )
    >>> # initialize weights
    >>> for weight in module.weight_views():
    ...     mul_1, mul_2, mul_out = weight.shape
    ...     a = jnp.sqrt(6 / (mul_1 * mul_2 + mul_out))
    ...     weight[:] = jax.random.uniform(jax.random.PRNGKey(0), shape=weight.shape, minval=-a, maxval=a)
    >>> n = 1000
    >>> vars = module(Irreps.randn(irreps_1, n, -1), Irreps.randn(irreps_2, n, -1)).var(0)
    >>> assert vars.min() > 1 / 3
    >>> assert vars.max() < 3
    """

    instructions: list[Any]
    shared_weights: bool
    internal_weights: bool
    weight_numel: int
    _did_compile_right: bool
    _specialized_code: bool
    _optimize_einsums: bool
    _in1_dim: int
    _in2_dim: int

    def __init__(
        self,
        irreps_in1: Irreps,
        irreps_in2: Irreps,
        irreps_out: Irreps,
        instructions: list[tuple],
        in1_var: Optional[Union[list[float], jnp.ndarray]] = None,
        in2_var: Optional[Union[list[float], jnp.ndarray]] = None,
        out_var: Optional[Union[list[float], jnp.ndarray]] = None,
        irrep_normalization: str = None,
        path_normalization: str = None,
        internal_weights: Optional[bool] = None,
        shared_weights: Optional[bool] = None,
        normalization=None,  # for backward compatibility
        _specialized_code: Optional[bool] = None,
        _optimize_einsums: Optional[bool] = None,
        name: Optional[str] = None,
    ) -> None:
        # === Setup ===
        super().__init__(name=name)

        if normalization is not None:
            warnings.warn(
                '`normalization` is deprecated. Use `irrep_normalization` instead.',
                DeprecationWarning,
            )
            irrep_normalization = normalization

        if irrep_normalization is None:
            irrep_normalization = 'component'

        if path_normalization is None:
            path_normalization = 'element'

        assert irrep_normalization in ['component', 'norm', 'none']
        assert path_normalization in ['element', 'path', 'none']

        self.irreps_in1 = Irreps(irreps_in1)
        self.irreps_in2 = Irreps(irreps_in2)
        self.irreps_out = Irreps(irreps_out)
        del irreps_in1, irreps_in2, irreps_out

        instructions = [x if len(x) == 6 else x + (1.0,) for x in instructions]
        instructions = [
            Instruction(
                i_in1=i_in1,
                i_in2=i_in2,
                i_out=i_out,
                connection_mode=connection_mode,
                has_weight=has_weight,
                path_weight=path_weight,
                path_shape={
                    'uvw': (
                        self.irreps_in1[i_in1].mul,
                        self.irreps_in2[i_in2].mul,
                        self.irreps_out[i_out].mul,
                    ),
                    'uvu': (self.irreps_in1[i_in1].mul, self.irreps_in2[i_in2].mul),
                    'uvv': (self.irreps_in1[i_in1].mul, self.irreps_in2[i_in2].mul),
                    'uuw': (self.irreps_in1[i_in1].mul, self.irreps_out[i_out].mul),
                    'uuu': (self.irreps_in1[i_in1].mul,),
                    'uvuv': (self.irreps_in1[i_in1].mul, self.irreps_in2[i_in2].mul),
                    'uvu<v': (
                        self.irreps_in1[i_in1].mul
                        * (self.irreps_in2[i_in2].mul - 1)
                        // 2,
                    ),
                    'u<vw': (
                        self.irreps_in1[i_in1].mul
                        * (self.irreps_in2[i_in2].mul - 1)
                        // 2,
                        self.irreps_out[i_out].mul,
                    ),
                }[connection_mode],
            )
            for i_in1, i_in2, i_out, connection_mode, has_weight, path_weight in instructions
        ]

        if in1_var is None:
            in1_var = [1.0 for _ in range(len(self.irreps_in1))]
        else:
            in1_var = [float(var) for var in in1_var]
            assert len(in1_var) == len(self.irreps_in1), (
                'Len of ir1_var must be equal to len(irreps_in1)'
            )

        if in2_var is None:
            in2_var = [1.0 for _ in range(len(self.irreps_in2))]
        else:
            in2_var = [float(var) for var in in2_var]
            assert len(in2_var) == len(self.irreps_in2), (
                'Len of ir2_var must be equal to len(irreps_in2)'
            )

        if out_var is None:
            out_var = [1.0 for _ in range(len(self.irreps_out))]
        else:
            out_var = [float(var) for var in out_var]
            assert len(out_var) == len(self.irreps_out), (
                'Len of out_var must be equal to len(irreps_out)'
            )

        def num_elements(ins):
            return {
                'uvw': (
                    self.irreps_in1[ins.i_in1].mul * self.irreps_in2[ins.i_in2].mul
                ),
                'uvu': self.irreps_in2[ins.i_in2].mul,
                'uvv': self.irreps_in1[ins.i_in1].mul,
                'uuw': self.irreps_in1[ins.i_in1].mul,
                'uuu': 1,
                'uvuv': 1,
                'uvu<v': 1,
                'u<vw': self.irreps_in1[ins.i_in1].mul
                * (self.irreps_in2[ins.i_in2].mul - 1)
                // 2,
            }[ins.connection_mode]

        normalization_coefficients = []
        for ins in instructions:
            mul_ir_in1 = self.irreps_in1[ins.i_in1]
            mul_ir_in2 = self.irreps_in2[ins.i_in2]
            mul_ir_out = self.irreps_out[ins.i_out]
            assert mul_ir_in1.ir.p * mul_ir_in2.ir.p == mul_ir_out.ir.p
            assert (
                abs(mul_ir_in1.ir.l - mul_ir_in2.ir.l)
                <= mul_ir_out.ir.l
                <= mul_ir_in1.ir.l + mul_ir_in2.ir.l
            )
            assert ins.connection_mode in [
                'uvw',
                'uvu',
                'uvv',
                'uuw',
                'uuu',
                'uvuv',
                'uvu<v',
                'u<vw',
            ]

            if irrep_normalization == 'component':
                alpha = mul_ir_out.ir.dim
            if irrep_normalization == 'norm':
                alpha = mul_ir_in1.ir.dim * mul_ir_in2.ir.dim
            if irrep_normalization == 'none':
                alpha = 1

            if path_normalization == 'element':
                x = sum(
                    in1_var[i.i_in1] * in2_var[i.i_in2] * num_elements(i)
                    for i in instructions
                    if i.i_out == ins.i_out
                )
            if path_normalization == 'path':
                x = in1_var[ins.i_in1] * in2_var[ins.i_in2] * num_elements(ins)
                x *= len([i for i in instructions if i.i_out == ins.i_out])
            if path_normalization == 'none':
                x = 1

            if x > 0.0:
                alpha /= x

            alpha *= out_var[ins.i_out]
            alpha *= ins.path_weight

            normalization_coefficients += [sqrt(alpha)]

        self.instructions = [
            Instruction(
                ins.i_in1,
                ins.i_in2,
                ins.i_out,
                ins.connection_mode,
                ins.has_weight,
                alpha,
                ins.path_shape,
            )
            for ins, alpha in zip(instructions, normalization_coefficients)
        ]

        self._in1_dim = self.irreps_in1.dim
        self._in2_dim = self.irreps_in2.dim

        if shared_weights is False and internal_weights is None:
            internal_weights = False

        if shared_weights is None:
            shared_weights = True

        if internal_weights is None:
            internal_weights = shared_weights and any(
                i.has_weight for i in self.instructions
            )

        assert shared_weights or not internal_weights
        self.internal_weights = internal_weights
        self.shared_weights = shared_weights

        opt_defaults = get_optimization_defaults()
        self._specialized_code = (
            _specialized_code
            if _specialized_code is not None
            else opt_defaults['specialized_code']
        )
        self._optimize_einsums = (
            _optimize_einsums
            if _optimize_einsums is not None
            else opt_defaults['optimize_einsums']
        )
        del opt_defaults

        # === Determine weights ===
        self.weight_numel = sum(
            prod(ins.path_shape) for ins in self.instructions if ins.has_weight
        )
        self.internal_weights = internal_weights

        # --- Output mask (static, non-trainable) ---
        if self.irreps_out.dim > 0:
            self.output_mask = jnp.concatenate(
                [
                    (
                        jnp.ones(mul * ir.dim)
                        if any(
                            (ins.i_out == i_out)
                            and (ins.path_weight != 0)
                            and (0 not in ins.path_shape)
                            for ins in self.instructions
                        )
                        else jnp.zeros(mul * ir.dim)
                    )
                    for i_out, (mul, ir) in enumerate(self.irreps_out)
                ]
            )
        else:
            self.output_mask = jnp.ones(0)

    def __repr__(self) -> str:
        npath = sum(prod(i.path_shape) for i in self.instructions)
        return (
            f'{self.__class__.__name__}'
            f'({self.irreps_in1.simplify()} x {self.irreps_in2.simplify()} '
            f'-> {self.irreps_out.simplify()} | {npath} paths | {self.weight_numel} weights)'
        )

    def _prep_weights(
        self, weight: Optional[Union[jnp.ndarray, list[jnp.ndarray]]]
    ) -> Optional[jnp.ndarray]:
        """Reshape and concatenate weight list if necessary."""
        if isinstance(weight, list):
            weight_shapes = [
                ins.path_shape for ins in self.instructions if ins.has_weight
            ]
            if not self.shared_weights:
                # Each weight must have batch dimension
                weight = [
                    w.reshape((-1, np.prod(shape)))
                    for w, shape in zip(weight, weight_shapes)
                ]
            else:
                weight = [
                    w.reshape(np.prod(shape)) for w, shape in zip(weight, weight_shapes)
                ]
            return jnp.concatenate(weight, axis=-1)
        else:
            return weight

    def _get_weights(self, weight: Optional[jnp.ndarray]) -> Optional[jnp.ndarray]:
        """Retrieve real weight tensor, either from argument or from internal parameters."""
        weight = self._prep_weights(weight)

        if weight is None:
            if self.weight_numel > 0 and not self.internal_weights:
                raise RuntimeError(
                    'Weights must be provided when the TensorProduct does not have internal_weights'
                )
            if self.internal_weights and self.weight_numel > 0:
                weight = hk.get_parameter(
                    'weight', [self.weight_numel], init=hk.initializers.RandomNormal()
                )
            else:
                return None
        else:
            if self.shared_weights:
                assert weight.shape == (self.weight_numel,), (
                    f'Invalid weight shape {weight.shape}'
                )
            else:
                assert weight.shape[-1] == self.weight_numel, (
                    f'Invalid weight shape {weight.shape}'
                )
                assert weight.ndim > 1, (
                    'When shared_weights is False, weights must have batch dimension'
                )
        return weight

    def right(self, y, weight: Optional[jax.Array] = None):
        r"""Partially evaluate :math:`w x \otimes y`.

        It returns an operator in the form of a tensor that can act on an arbitrary :math:`x`.

        For example, if the tensor product above is expressed as

        .. math::

            w_{ijk} x_i y_j \rightarrow z_k

        then the right method returns a tensor :math:`b_{ik}` such that

        .. math::

            w_{ijk} y_j \rightarrow b_{ik}

        .. math::

            x_i b_{ik} \rightarrow z_k

        The result of this method can be applied with a tensor contraction:

        .. code-block:: python

            torch.einsum("...ik,...i->...k", right, input)

        Parameters
        ----------
        y : `torch.Tensor`
            tensor of shape ``(..., irreps_in2.dim)``

        weight : `torch.Tensor` or list of `torch.Tensor`, optional
            required if ``internal_weights`` is ``False``
            tensor of shape ``(self.weight_numel,)`` if ``shared_weights`` is ``True``
            tensor of shape ``(..., self.weight_numel)`` if ``shared_weights`` is ``False``
            or list of tensors of shapes ``weight_shape`` / ``(...) + weight_shape``.
            Use ``self.instructions`` to know what are the weights used for.

        Returns
        -------
        `torch.Tensor`
            tensor of shape ``(..., irreps_in1.dim, irreps_out.dim)``
        """
        assert self._did_compile_right, (
            '`right` method not compiled, set compile_right=True'
        )
        assert y.shape[-1] == self._in2_dim

        weight = self._get_weights(weight)

        return codegen_tensor_product_right(
            y,
            weight,
            self.irreps_in1,
            self.irreps_in2,
            self.irreps_out,
            self.instructions,
            self.shared_weights,
            self._specialized_code,
        )

    def __call__(self, x, y, weight: Optional[jax.Array] = None):
        r"""Evaluate :math:`w x \otimes y`.

        Parameters
        ----------
        x : `torch.Tensor`
            tensor of shape ``(..., irreps_in1.dim)``

        y : `torch.Tensor`
            tensor of shape ``(..., irreps_in2.dim)``

        weight : `torch.Tensor` or list of `torch.Tensor`, optional
            required if ``internal_weights`` is ``False``
            tensor of shape ``(self.weight_numel,)`` if ``shared_weights`` is ``True``
            tensor of shape ``(..., self.weight_numel)`` if ``shared_weights`` is ``False``
            or list of tensors of shapes ``weight_shape`` / ``(...) + weight_shape``.
            Use ``self.instructions`` to know what are the weights used for.

        Returns
        -------
        `torch.Tensor`
            tensor of shape ``(..., irreps_out.dim)``
        """
        assert x.shape[-1] == self._in1_dim
        assert y.shape[-1] == self._in2_dim

        weight = self._get_weights(weight)

        return codegen_tensor_product_left_right(
            x,
            y,
            weight,
            self.irreps_in1,
            self.irreps_in2,
            self.irreps_out,
            self.instructions,
            self.shared_weights,
            self._specialized_code,
        )

    def weight_view_for_instruction(
        self, instruction: int, weight: Optional[jnp.ndarray] = None
    ) -> jnp.ndarray:
        """Return the weights corresponding to a given instruction."""
        ins = self.instructions[instruction]

        if not ins.has_weight:
            raise ValueError(f'Instruction {instruction} has no weights.')

        # Get the effective weights (either passed in or internal param)
        weight = self._get_weights(weight)

        # Compute offset in the flattened weight vector
        offset = sum(
            math.prod(prev_ins.path_shape)
            for prev_ins in self.instructions[:instruction]
        )
        flatsize = math.prod(ins.path_shape)

        # Slice out the relevant chunk
        sliced = weight[..., offset : offset + flatsize]

        # Reshape to (..., *ins.path_shape)
        batchshape = weight.shape[:-1]
        return jnp.reshape(sliced, batchshape + ins.path_shape)

    def weight_views(
        self, weight: Optional[jax.Array] = None, yield_instruction: bool = False
    ):
        r"""Iterator over weight views for each weighted instruction.

        Parameters
        ----------
        weight : `jax.Array`, optional
            like ``weight`` argument to ``forward()``

        yield_instruction : `bool`, default False
            Whether to also yield the corresponding instruction.

        Yields
        ------
        If ``yield_instruction`` is ``True``, yields ``(instruction_index, instruction, weight_view)``.
        Otherwise, yields ``weight_view``.
        """
        weight = self._get_weights(weight)
        batchshape = weight.shape[:-1]
        offset = 0

        for ins_i, ins in enumerate(self.instructions):
            if ins.has_weight:
                flatsize = math.prod(ins.path_shape)
                # Slice the relevant portion
                this_weight = weight[..., offset : offset + flatsize]
                # Reshape to match instruction path shape
                this_weight = jnp.reshape(this_weight, batchshape + ins.path_shape)
                offset += flatsize

                if yield_instruction:
                    yield ins_i, ins, this_weight
                else:
                    yield this_weight

    def visualize(
        self,
        weight: Optional[jax.Array] = None,
        plot_weight: bool = True,
        aspect_ratio=1,
        ax=None,
    ):  # pragma: no cover
        r"""Visualize the connectivity of this `e3nn.o3.TensorProduct`

        Parameters
        ----------
        weight : `torch.Tensor`, optional
            like ``weight`` argument to ``forward()``

        plot_weight : `bool`, default True
            Whether to color paths by the sum of their weights.

        ax : ``matplotlib.Axes``, default None
            The axes to plot on. If ``None``, a new figure will be created.

        Returns
        -------
        (fig, ax)
            The figure and axes on which the plot was drawn.
        """
        import numpy as np

        def _intersection(x, u, y, v):
            u2 = np.sum(u**2)
            v2 = np.sum(v**2)
            uv = np.sum(u * v)
            det = u2 * v2 - uv**2
            mu = np.sum((u * uv - v * u2) * (y - x)) / det
            return y + mu * v

        import matplotlib
        import matplotlib.pyplot as plt
        from matplotlib import patches
        from matplotlib.path import Path

        if ax is None:
            ax = plt.gca()

        fig = ax.get_figure()

        # hexagon
        verts = [
            np.array([np.cos(a * 2 * np.pi / 6), np.sin(a * 2 * np.pi / 6)])
            for a in range(6)
        ]
        verts = np.asarray(verts)

        # scale it
        if not (aspect_ratio in ['auto'] or isinstance(aspect_ratio, (float, int))):
            raise ValueError(
                f"aspect_ratio must be 'auto' or a float or int, got {aspect_ratio}"
            )

        if aspect_ratio == 'auto':
            factor = 0.2 / 2
            min_aspect = 1 / 2
            h_factor = max(len(self.irreps_in2), len(self.irreps_in1))
            w_factor = len(self.irreps_out)
            if h_factor / w_factor < min_aspect:
                h_factor = min_aspect * w_factor
            verts[:, 1] *= h_factor * factor
            verts[:, 0] *= w_factor * factor

        if isinstance(aspect_ratio, (float, int)):
            factor = 0.1 * max(
                len(self.irreps_in2), len(self.irreps_in1), len(self.irreps_out)
            )
            verts[:, 1] *= factor
            verts[:, 0] *= aspect_ratio * factor

        codes = [
            Path.MOVETO,
            Path.LINETO,
            Path.MOVETO,
            Path.LINETO,
            Path.MOVETO,
            Path.LINETO,
        ]

        path = Path(verts, codes)
        patch = patches.PathPatch(path, facecolor='none', lw=1, zorder=2)
        ax.add_patch(patch)

        n = len(self.irreps_in1)
        b, a = verts[2:4]

        c_in1 = (a + b) / 2
        s_in1 = [a + (i + 1) / (n + 1) * (b - a) for i in range(n)]

        n = len(self.irreps_in2)
        b, a = verts[:2]

        c_in2 = (a + b) / 2
        s_in2 = [a + (i + 1) / (n + 1) * (b - a) for i in range(n)]

        n = len(self.irreps_out)
        a, b = verts[4:6]

        s_out = [a + (i + 1) / (n + 1) * (b - a) for i in range(n)]

        # get weights
        if weight is None and not self.internal_weights:
            plot_weight = False
        elif plot_weight:
            path_weight = []
            for ins_i, ins in enumerate(self.instructions):
                if ins.has_weight:
                    this_weight = self.weight_view_for_instruction(ins_i, weight=weight)
                    path_weight.append(jnp.mean(jnp.square(this_weight)).item())
                else:
                    path_weight.append(0)
            path_weight = np.asarray(path_weight)
            path_weight /= np.abs(path_weight).max()
        cmap = matplotlib.colormaps['Blues']

        for ins_index, ins in enumerate(self.instructions):
            y = _intersection(s_in1[ins.i_in1], c_in1, s_in2[ins.i_in2], c_in2)

            verts = []
            codes = []
            verts += [s_out[ins.i_out], y]
            codes += [Path.MOVETO, Path.LINETO]
            verts += [s_in1[ins.i_in1], y]
            codes += [Path.MOVETO, Path.LINETO]
            verts += [s_in2[ins.i_in2], y]
            codes += [Path.MOVETO, Path.LINETO]

            if plot_weight:
                color = (
                    cmap(0.5 + 0.5 * path_weight[ins_index])
                    if ins.has_weight
                    else 'black'
                )
            else:
                color = 'green' if ins.has_weight else 'black'

            ax.add_patch(
                patches.PathPatch(
                    Path(verts, codes),
                    facecolor='none',
                    edgecolor=color,
                    alpha=0.5,
                    ls='-',
                    lw=1.5,
                )
            )

        # add labels
        padding = 3
        fontsize = 10

        def format_ir(mul_ir) -> str:
            if mul_ir.mul == 1:
                return f'${mul_ir.ir}$'
            return f'${mul_ir.mul} \\times {mul_ir.ir}$'

        for i, mul_ir in enumerate(self.irreps_in1):
            ax.annotate(
                format_ir(mul_ir),
                s_in1[i],
                horizontalalignment='right',
                textcoords='offset points',
                xytext=(-padding, 0),
                fontsize=fontsize,
            )

        for i, mul_ir in enumerate(self.irreps_in2):
            ax.annotate(
                format_ir(mul_ir),
                s_in2[i],
                horizontalalignment='left',
                textcoords='offset points',
                xytext=(padding, 0),
                fontsize=fontsize,
            )

        for i, mul_ir in enumerate(self.irreps_out):
            ax.annotate(
                format_ir(mul_ir),
                s_out[i],
                horizontalalignment='center',
                verticalalignment='top',
                rotation=90,
                textcoords='offset points',
                xytext=(0, -padding),
                fontsize=fontsize,
            )

        ax.set_xlim(-2, 2)
        ax.set_ylim(-2, 2)
        ax.axis('equal')
        ax.axis('off')

        return fig, ax
