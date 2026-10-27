# Offline installation model

`verification_add.onnx` is a 2×2 float32 CPU test graph: `y = Add(x, x)`.
It uses ONNX IR 8 and standard operator set 13, with no external weights or data.
The expected result for `[[1,2],[3,4]]` is `[[2,4],[6,8]]`.

The tiny protobuf was authored directly using the field definitions in the
[official ONNX schema](https://github.com/onnx/onnx/blob/main/onnx/onnx.proto).
It exercises ONNX Runtime graph loading and numerical execution in source,
portable and installed verification, without an ONNX authoring dependency.
