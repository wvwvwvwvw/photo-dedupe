# 环境适配指南（换电脑 / 装依赖 / 开 GPU）

> 目标：把这套程序搬到**任意一台 Windows 电脑**上，不需要人工摸索。
> 按本文执行；最后一步永远是跑 `--selfcheck --json`，看 `"ok": true`。
>
> 所有命令都假定**当前目录 = 程序目录**（`app.py` 所在）。路径含中文/空格都没问题
> （已实测：路径含空格、中文、括号都能正常跑）。

---

## 0. 需要什么

| 用途 | 依赖 | 版本 | 是否随包 |
| --- | --- | --- | --- |
| 运行 | Python | 3.10–3.13（实测 3.12.14 embeddable） | ✅ `runtime\` |
| 运行 | Pillow / numpy | 12.3.0 / 2.3.5 | ✅ 已装 |
| 图片 GPU 加速 | torch + torchvision | CUDA 版（实测 2.14.1+cu130 / 0.29.1） | ✅ 已装（NVIDIA 显存加速） |
| 图片 GPU 备用 | torch-directml | 任意（会替换上面的 CUDA 版） | ❌ 可选，见第 2 节 |
| 视频/音频 GPU | onnxruntime | 1.24.4（含 DirectML） | ✅ 已装（AMD/Intel/NVIDIA 都行） |
| 压缩/解压 | 7za | 23.01 | ✅ `vendor\7z\7za.exe` |
| 视频抽帧 | ffmpeg | 7.1 | ✅ `vendor\ffmpeg.exe` |
| 视频特征 | mobilenetv2 模型 | 14 MB | ✅ `vendor\mobilenetv2-7-feat.onnx` |
| 破解 | hashcat | 8.0.0 | ✅ `vendor\hashcat\hashcat-8.0.0` |
| 明文攻击 | bkcrack | 1.8.1 | ✅ `vendor\bkcrack` |
| 繁简 | OpenCC 字典 | — | ✅ `vendor\opencc\` |
| RAR | WinRAR / 完整版 7-Zip | — | ❌ 可选（自带 7za 不支持 RAR） |
| 更多图片格式 | pillow-heif / rawpy 等 | — | ❌ 可选 |

程序**不联网**；除音视频与压缩包外只用标准库 + Pillow/numpy。

---

## 1. 迁移到另一台电脑

```powershell
# 1) 整个目录拷过去（含 runtime\、vendor\、data\、web\）
#    建议放 D:\ 这类有写权限的位置，别放 C:\Program Files

# 2) 自检
.\runtime\python.exe app.py --selfcheck          # 人看
.\runtime\python.exe app.py --selfcheck --json   # 脚本看，检查 "ok": true

# 3) 启动
.\启动.bat
```

自检会打印：内置运行时是否生效、torch/CUDA、ffmpeg/模型、onnxruntime 的 provider、
图片格式数量、数据目录可写性，以及一行**加速能力**：

```
加速能力    : 图片查重 GPU（CUDA） · 视频/音频 GPU（DirectML）
```

判断标准：

| 情况 | 说明 |
| --- | --- |
| `ok: true` | 可以直接用 |
| `torch: 未安装` | 图片查重走 CPU，其它功能正常 |
| `加速能力: 图片查重 CPU · 视频/音频 GPU（DirectML）` | AMD/Intel 机器，正常现象 |
| `目录可写: 否` | 换个目录（需要管理员权限的位置不行） |

---

## 2. 没有 `runtime\` 时手动装（一般用不到）

```powershell
python -m pip install -r requirements.txt
# 或最小集合
python -m pip install pillow numpy pyzipper pycryptodomex onnxruntime
```

GPU（可选）：

```powershell
# NVIDIA：按显卡驱动对应 CUDA 版本装（能吃 nvJPEG，真正加速图片解码）
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130
# 视频特征加速（任意 DX12 显卡）
python -m pip install onnxruntime-directml
```

装好后 `--selfcheck` 应显示 `torch ... CUDA=True`。

### 非 NVIDIA 显卡：DirectML 备用方案

DirectML 版 torch 和 CUDA 版不能共存（装一个会顶掉另一个），程序按
**CUDA → DirectML → CPU** 的顺序选路。想给 AMD / Intel 的机器开图片加速：

```powershell
# 用界面上现成的脚本（会二次确认）
.\安装 DirectML 加速.bat

# 或者手动
.
untime\python.exe -m pip install torch-directml
.
untime\python.exe app.py --selfcheck      # 加速能力应显示 GPU（DirectML）
```

注意：装完 CUDA 加速就没了；想换回去再装 cu130 版 torch。
只想试又不想动当前运行时：把另一个装了 DML 的 Python 路径写进 `gpu_python.txt`。

> 如果不想动本机 Python，也可以用 `gpu_python.txt`：文件里写另一个 Python 的完整路径
> （例如某个装了 CUDA torch 的解释器），`app.py` 启动时会转过去。路径不存在会自动忽略。

---

## 3. 换电脑后 GPU 相关

* **NVIDIA 机器**：拷过去就能用 GPU（CUDA 库都在 `runtime\Lib\site-packages\torch\lib`）。
  驱动太老会看到 `torch.cuda.is_available() == False`，升级驱动即可
* **AMD / Intel 机器**：图片查重自动走 CPU；视频特征走 DirectML（要 `onnxruntime` 带 DML provider）
* **什么显卡都没有**：全部 CPU。启动会慢 1–2 秒（torch 探测），之后正常
* 想测本机最优配置：`.\runtime\python.exe bench.py --write`

---

## 4. 体积控制

| 场景 | 做法 | 结果 |
| --- | --- | --- |
| 只要 CPU 图片查重 | `runtime\python.exe -m pip uninstall -y torch torchvision` | `runtime\` 3.2 GB → 约 230 MB |
| 不要破解 | 删 `vendor\hashcat`、`dicts\` | −约 150 MB |
| 不要视频查重 | 删 `vendor\ffmpeg.exe`、`vendor\mobilenetv2-7-feat.onnx` | −约 100 MB |
| 清数据库 | 删 `data\library.db*`（会丢索引）、`data\thumbs`、`data\previews` | 视图库而定 |

精简后功能对照：删 torch 只影响图片 GPU 加速；删 ffmpeg 则视频/音频查重不可用。

---

## 5. 便携性验证清单

换机器后按这个顺序点一遍（每项都能在界面上完成）：

1. 启动 → 左下角出现引擎状态，没有报错
2. 「图片」→ 添加一个文件夹 → 开始扫描 → 出组 → 点 `⤢` 对比窗口能开
3. 「视频」→ 添加含视频的目录 → 扫描 → 行上 ▶ 能播放
4. 「打包」→ 选输入/输出 → 运行流程 → 日志显示写入成功
5. 「解压」→ 解那个包 → 文件出来
6. 「繁简编码」→ 粘一段繁体 → 点试一下 → 出简体
7. 「存档」→ 保存 → 列表出现 → 载入成功
8. 「设置」→ 改占用 40% → 保存 → 下次扫描变慢但更安静

命令行版本（自动化）：

```powershell
.\runtime\python.exe app.py --selfcheck --json
.\runtime\python.exe _test\test_pack.py
.\runtime\python.exe _test\test_archive.py
.\runtime\python.exe _test\test_media_file.py
```

---

## 6. 常见故障

| 症状 | 原因 / 处理 |
| --- | --- |
| 双击 `启动.bat` 闪一下就没了 | 在 cmd 里跑 `runtime\python.exe app.py` 看报错；多半是端口被占用（会自动换）或目录不可写 |
| 浏览器打不开 127.0.0.1:8765 | 看黑窗口里的实际端口；`--port` 可指定 |
| `目录可写: 否` | 换到有写权限的目录 |
| 视频页 500 / 点视频只有文件名 | 服务端是旧代码，重启程序 |
| RAR 解不开 | 装 WinRAR 或完整版 7-Zip（自带 7za 不支持 RAR） |
| 显卡没被用上 | `--selfcheck` 看 torch/CUDA；NVIDIA 需要驱动支持对应 CUDA 版本 |
| 破解很慢 | 没有 NVIDIA 显卡时走 CPU 校验；或把 hashcat 指到独显（`vendor\hashcat`） |
