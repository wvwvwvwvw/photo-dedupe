# 测试说明

这些是开发时的自测脚本，**不是**完整的单元测试框架，运行方式很直接：

```bat
:: 在仓库根目录
python tests\test_core.py
python tests\test_media_chain.py
```

## 两类测试

| 类型 | 需要什么 | 例子 |
| --- | --- | --- |
| **自包含**（推荐先跑） | 只要依赖装好，自己造临时文件 | `test_core.py`（合成图）、`test_media_chain.py`（打分矩阵）、`test_i18n.py`（字典覆盖）、`test_waveform.py`（ffmpeg 造音频）、`test_archive.py`、`test_patch_on_demand.py`、`test_other_files.py`、`test_media_file.py` |
| **需要样本库** | `data\library.db` 里有真实照片/媒体记录（即你自己扫过一次） | `test_patch_on_demand.py` 的一部分、`test_dml_backend.py`、`test_index.py` 里的大库用例 |

没有样本时这类脚本会打印"跳过"或直接返回，不会破坏数据。

## 会自动造数据的那几个

* `test_core.py` —— 用 Pillow 画几张图，跑分组的端到端
* `test_waveform.py` —— 用 ffmpeg 生成正弦波，测波形接口
* `test_media_file.py` —— 造一个假 mp4，测 Range 流式响应
* `test_other_files.py` —— 造一个超小视频 + 动图，验证"其他文件"记录不被清理
* `test_dml_backend.py` —— 注入假的 `torch_directml`，验证选路 CUDA→DML→CPU

## 注意

* 部分脚本会写 `data\` 下的临时文件，跑完记得看一眼有没有残留（一般会自动清理）
* `test_pack.py` / `test_pipeline.py` 会在系统临时目录里打包解包，不会动你的照片
