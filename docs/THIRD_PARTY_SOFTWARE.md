# Third-party software

Third-party licenses and notices remain applicable when distributing TEMPO's source or installer. This document does not assign a license to TEMPO itself.

| Component | Declared licensing | Reference |
| --- | --- | --- |
| PyQt6 | GPLv3 or a Riverbank commercial license | [Riverbank](https://en.riverbankcomputing.com/software/pyqt) |
| Ultralytics | AGPL-3.0 or a separate Ultralytics license | [Ultralytics](https://www.ultralytics.com/license) |
| Ultralytics platform SDK | AGPL-3.0-only | [Package](https://pypi.org/project/ultralytics-platform/0.1.90/) |
| PyTorch | BSD-style, Apache-2.0 and component notices | [License](https://github.com/pytorch/pytorch/blob/v2.14.1/LICENSE) |
| TensorFlow | Apache-2.0 | [License](https://github.com/tensorflow/tensorflow/blob/master/LICENSE) |
| StarDist | BSD-3-Clause | [License](https://github.com/stardist/stardist/blob/main/LICENSE.txt) |
| CSBDeep | BSD-3-Clause | [License](https://github.com/CSBDeep/CSBDeep/blob/main/LICENSE.txt) |
| Cellpose | BSD-style | [License](https://github.com/MouseLand/cellpose/blob/main/LICENSE) |
| OpenCV | Apache-2.0, with component notices | [License](https://github.com/opencv/opencv/blob/40738fb16ceddb5fb3fea747585f7ce6abb0605b/LICENSE) |
| pymmcore | LGPL-2.1-only | [Versioned license](https://github.com/micro-manager/pymmcore/blob/v12.5.1.75.0/LICENSE.txt) |
| Hugging Face Hub | Apache-2.0 | [License](https://github.com/huggingface/huggingface_hub/blob/main/LICENSE) |
| Gitleaks, development scanner | MIT | [License](https://github.com/gitleaks/gitleaks/blob/master/LICENSE) |

Windows dependency versions are pinned in the lockfile and recorded in build provenance. The table lists major components; version-specific notices accompany the installer.

Windows packaging collects installed dependency license and notice files into `third-party-licenses` in the bundled module directory. Its index identifies package names, versions, and any packages with no collected license file. The inventory also includes build-time dependencies.

The adjacent `third-party-sources` directory provides version-specific upstream source links and checksums. The maintained source manifest is `installer/dependency-sources.json`; source retrieval and release requirements are described in [Windows packaging](PACKAGING_AND_DISTRIBUTION.md#matching-source).

For dependency versions whose wheels omit notices, upstream license files and their source URLs/checksums are maintained with the assets. These cover ANTLR's Python runtime, cloudpickle, FastRemap, FlatBuffers, NVIDIA's NVML Python bindings, pymmcore, and the TensorBoard data-server package. Fallback notices are matched to exact package versions. Windows builds reject an incomplete notice inventory.

TEMPO's original software is licensed under AGPL-3.0-only. Third-party components and reference images retain the licenses and notices described here and included with the distribution.

Model checkpoints and datasets can have terms separate from their library's code license. Access to the source does not provide access to the private research workspace or its contents.

## MUSCLEMOTION

TEMPO's motion-analysis work draws on MUSCLEMOTION, the ImageJ macro developed by Luca Sala, Berend J. van Meer, Francis L. Burton, and colleagues at Leiden University Medical Center and the University of Glasgow. Credit for the original method and macro belongs to its authors. TEMPO uses its own Python analysis pipeline; its measurements should not be assumed to be interchangeable with those of the ImageJ macro.

Sala L., van Meer B. J., et al. **MUSCLEMOTION: A Versatile Open Software Tool to Quantify Cardiomyocyte and Cardiac Muscle Contraction In Vitro and In Vivo.** *Circulation Research*. 2018;122(3):e5–e16. [doi:10.1161/CIRCRESAHA.117.312067](https://doi.org/10.1161/CIRCRESAHA.117.312067).

The original software and its GPLv3 license are available in the [MUSCLEMOTION repository](https://github.com/l-sala/MUSCLEMOTION). The standalone ImageJ macro is not bundled with TEMPO.

## Annotation reference micrographs

The annotation helper includes cropped views from Figure 2A and 2E of:

Omatsu-Kanbe M., Yoshioka K., Fukunaga R., Sagawa H., Matsuura H. **A simple antegrade perfusion method for isolating viable single cardiomyocytes from neonatal to aged mice.** *Physiological Reports*. 2018;6(9):e13688. [doi:10.14814/phy2.13688](https://doi.org/10.14814/phy2.13688).

© 2018 The Authors. *Physiological Reports* published by Wiley Periodicals, Inc. on behalf of The Physiological Society and the American Physiological Society. Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

The original figure is retained unchanged. The helper crops panel A and the 1-hour and 5-hour images from panel E, retaining their scale bars. The images were retrieved from the NLM/PMC Article Datasets on 20 October 2026; the bundled copy reflects that retrieval date. The asset manifest records the source URL, checksum, and display crops. Links to other publications do not imply that their images are bundled.

## DINOv3 / CPDINO backbone

The controlled Cellpose experiments use Meta DINOv3 through CPDINO. The source is pinned to commit `6876159a11b4df116f30f667f8c9888617df0751` (package version 0.0.1) under the [DINOv3 License](https://github.com/facebookresearch/dinov3/blob/6876159a11b4df116f30f667f8c9888617df0751/LICENSE.md). The build retains the upstream source checksum and includes its packaged license notice. Research publications should acknowledge DINOv3 alongside Cellpose.
