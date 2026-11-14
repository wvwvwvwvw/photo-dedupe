# 项目说明：设计与实现

> 最后更新：2026-11-12 · 便携包 3.7 GB（runtime 3.2 GB 含 torch+CUDA，精简后可到 ~0.5 GB）

使用说明在 [`README.md`](README.md)；环境安装 / 换电脑见 [`AGENT_SETUP.md`](AGENT_SETUP.md)。
本文只讲**内部怎么实现、为什么这么做**。

---

## 一、整体架构

```
浏览器 (127.0.0.1:8765)
   │  fetch /api/*
   ▼
app.py  ←─ ThreadingHTTPServer + Handler（路由、静态文件、JSON、流式文件）
   │
   ├─ App          业务门面：扫描任务、分组缓存、存档、设置、媒体、工具
   │   ├─ dedupe\  图片查重
   │   └─ media\   媒体查重 + 打包/解压/转换/过滤/破解
   │
   ├─ data\library.db   SQLite（images / roots / media_* / meta）
   └─ data\            缩略图、预览图、存档、密码本、日志
```

设计取向：

* **单进程、多线程**。没有额外服务、没有数据库安装，全部塞进一个目录
* **前端是静态文件**（`web\`），后端只提供 JSON + 少量二进制（缩略图/预览/媒体流）
* **长任务都是后台线程 + 状态轮询**（扫描、打包、解压、转换、过滤、破解都是这个模式）
* **中间产物尽量留在内存**，最后一个动作才写盘（打包/解压都是）

---

## 二、目录与模块

```
app.py            ~2900 行：HTTP 路由 + App 门面（扫描/分组/存档/媒体/工具/自检）
bench.py          测速脚本（CPU 各线程数 / GPU / 混合，--write 写回 settings.json）
settings.json     引擎、线程、占用、存档位置、落库频率、bench 结果
web\              index.html（结构）· app.js（~3700 行逻辑）· style.css（设计系统）
dedupe\
  hashing.py      单图分析：draft 解码 → 512 长边 → pHash/pHash_c/dHash/aHash/清晰度/主色/16×16 指纹
  gpupipe.py      GPU 单图分析（torch，nvJPEG 解码 + 显存里算）
  hybrid.py       CPU+GPU 混合引擎（两个队列 + work stealing）
  pipeline.py     引擎门面：CpuEngine / GpuEngine / make_engine / verify_engine / INFO
  index.py        哈希索引：全量矩阵、分桶查询、整块矩阵、星形聚类
  grouping.py     prepare()（三道关 + 聚类）/ render()（分页出组）/ select_members()
  store.py        SQLite 读写、增量、落库、迁移、导出（存档用）
  archive.py      存档 JSON 的保存/读取/导入/删除
  patches.py      灰度指纹补算（按需、限速、带进度）
  formats.py      图片格式能力探测
  runtime.py      gpu_python.txt 转发
media\
  scan.py         媒体枚举 + 分析任务（含"跳过"记录）
  video.py        抽帧 → ONNX 特征 + pHash
  audio.py        频谱峰值指纹
  novel.py        书名规范化/章节解析/正文指纹
  features.py     ONNX 会话（DirectML/CPU）、pHash、帧匹配分
  grouping.py     视频/音频/小说分组（星形聚类）
  store.py        media_items / media_roots
  pack.py         打包（zip 多线程 zlib、7z、AES、拆分）
  unzip.py        解压（内存逐层、分卷、密码、乱码修复）
  conv.py         繁简/编码转换；clean.py 小说广告过滤
  docs.py/cfb.py  PDF / Office 加密校验
  hashcat.py      显卡破解封装；sevenzip.py 7z 封装；ffmpeg.py 媒体工具封装
```

---

## 三、图片查重

### 3.1 单图分析（`hashing.analyze_image`）

1. `Image.open` + `draft("RGB", (512,512))`：JPEG 直接按 1/8 解码（4K 图只解 0.5 MP）
2. EXIF 方向校正 → 只做**一次**缩放（长边 512）→ 之后所有指标都基于这张小图
3. 产出：pHash、pHash_c（中心裁 6% 再算，容忍裁边）、dHash、aHash、清晰度、主色、16×16 灰度指纹

### 3.2 引擎（`pipeline` / `hybrid`）

| 模式 | 实现 |
| --- | --- |
| `cpu` | `CpuEngine`：PIL draft 解码，N 线程 |
| `gpu` | `GpuEngine`：torch + nvJPEG 全分辨率解码，显存里 resize/灰度/DCT |
| `auto`（默认） | `HybridEngine`：JPEG 队列给 GPU 线程，其他格式给 CPU 池；两边共享队列，快的自然多拿 |

选择逻辑：`make_engine()` 先 `gpu_available()` 探测，再用真实样本 `verify_engine()` 校验
（dHash/pHash 与纯 CPU 比对，`max_dhash>12 / max_phash>12 / mean>4` 就判定 GPU 路径有问题并回退），
最后按 `load` 百分比换算 CPU 线程数、给 GPU 加间歇休眠（`work×(100-load)/load`）。

`load` 实测（混合引擎、300 张真实照片）：100%→237、80%→189、60%→135、40%→96、20%→50 张/秒。

### 3.3 判重（`grouping.prepare`）

```
候选：pHash/pHash_c 四组合取最小汉明距离 ≤ 阈值（全量矩阵 或 分桶检索）
  ↓
dHash 校验：dhash ≤ 16 或 pHash ≤ 4
  ↓
灰度指纹：16×16 归一化相关系数 ≥ 0.96（缺失的行自动跳过这一关）
  ↓
星形聚类：种子按(像素×清晰度)排序，成员必须直接与种子相似 ≥ 阈值
```

* 连通分量会把 A~B~C 串成一组；星形聚类把簇直径限制在 2×阈值内
* 6.8 万张实测：最大组 305→27，≥30 的组 5→0，召回 99.9%→98.4%

### 3.4 检索规模（`index`）

| n | 路径 | 复杂度 | 实测 |
| --- | --- | --- | --- |
| ≤ 12 万 | `_edges_queries`：5 段 × 12 位分桶 + 邻桶探测 | O(n·k) | 6.8 万 3.5s |
| > 12 万 | `_edges_blocks`：按宽高比分片 → 桶内整块矩阵 | O(n²/分片) | 50 万 4.7s / 100 万 20s |

两处关键优化：

* `_popcount` 用 numpy 2.x 的 `bitwise_count`（比查表快约 5 倍），`min4_numpy` 也受益
* 候选对**连同距离**缓存（`_CAND_CACHE`，上限 2 个图库、距离 ≤ 20）：拖动阈值滑块从 7s 变 0.3s

---

## 四、媒体查重（视频 / 音频 / 小说）

| 类型 | 特征 | 门槛 | 阈值 |
| --- | --- | --- | --- |
| 视频 | 8 帧 × MobileNetV2 1280 维（ONNX，DirectML/CPU） | 时长差 >3% 不比；首帧 pHash 差 >26 不比 | 0.88 |
| 音频 | 8 kHz 频谱峰值指纹（3 段抽样窗口） | 时长差 >5% 不比；抽样哈希重合太少不比 | 0.35 |
| 小说 | 规范化书名相似度 + 章节数 | 书名长度差 >8 不比 | 0.82 |

三种都是**星形聚类**（成员直接与代表相似），避免"系列名/相似片段"把不同文件串成一组。
小说另有正文比对：章节序列重合度 ≥0.88 且字数差 ≤20% 才标"正文重复"。

扫描任务（`MediaScanJob`）除了分析成功的记录，还会把**没进查重的文件**记成
`status='skipped'`（太小 / 动图 / 没音轨 / 打不开），前端「其他文件」直接读这些行。

---

### 四·补、界面多语言（web/i18n.js）

* 做法：以**中文原文为 key** 的短语表（`EN` / `JA`），替换文本节点和
  `placeholder/title/alt/aria-label`；`WeakMap` 记住原文，切回中文能原样恢复
* 动态内容：`MutationObserver`（节流到一帧）增量翻译新节点
* 数字类短语（"68,059 张图片"）用正则规则（`RULES`）处理
* 默认 `auto` = 按 `navigator.language` 选；选择存 `localStorage['pd.lang']`
* 没收录的短语保留中文（安全兜底），后端返回的细节文案不翻译
* 测试：`_test/test_i18n.py` 检查关键短语是否两本字典都覆盖、选择器位置、脚本引入

### 四·补二、批量删除的增量更新

以前删完要 `loadGroups()` 重拉整页：6.8 万张要几秒，删几百个文件时每批都卡。

现在：
* 前端把选中的 id 按 **40 个一批**（媒体 20 个）发请求
* 每批成功后就地改本地数据（`dropIdsFromGroups` / `mDropIds`），组里少于 2 张就整组消失，
  统计数字同步减；**不重拉列表**
* 最后只调一次轻量的 `/api/stats` 校准，`/api/roots` 刷新文件夹计数

## 五、打包 / 解压 / 破解

* **打包**：`media/pipeline.py` 把步骤列表翻译成一串动作，中间结果都是 `bytes`
  * zip：多线程 zlib（≤16 线程），已压缩格式（jpg/png/mp4…）直接 STORE
  * AES：pyzipper；ZipCrypto：自己写（比 pyzipper 快 3-10 倍）
  * 7z：`vendor\7z\7za.exe` 临时目录往返
  * 拆分：`split_parts()` 计算卷大小，`write_split()` 按后缀风格写卷
* **解压**：`media/unzip.py` 并行读条目 + 并行写，分卷（`.z01`/`.001`）先合并，
  cp437 乱码名修复，密码按"密码本 → 已破解密码 → 逐层试"的顺序试
* **破解**：`media/hashcat.py` 封装 hashcat（NVIDIA，`-m` 模式表、掩码、字典、变形），
  没有显卡时用 `media/docs.py` 的 CPU 校验（PDF R5 约 750k/s/核）；7z 走 hashcat 11600

---

### 五·补、媒体预览的波形

`media/ffmpeg.waveform()` 用 ffmpeg 把整段音频解成 **4 kHz 单声道**（`-t 1800` 封顶），
numpy 按桶 reshape 后取 `min/max`，归一化到 int16 的 -127~127，返回交错数组。
`GET /api/media/waveform?id=&buckets=` 附带 8 条内存缓存（id+mtime+桶数）。
前端画在 `<canvas>` 上，按峰值自动归一化，点/拖可 `currentTime = ratio × duration`，
`timeupdate` 时重画进度线；视频预览也走同一套（有音轨就画）。

坑：视频文件里 `0:0` 是画面，`-map 0:0` 会解不出音频 → 先试 `0:a:N`，失败再退回 `0:N`。
播放器选择也按**文件后缀**而不是页面类型（音频页里的 mp4 要用 `<video>`）。

## 六、数据模型（SQLite `data/library.db`）

```sql
images(id, path UNIQUE, root, name, size, mtime, w, h, fmt, pix,
       phash, phash_c, trim, dhash, ahash, sharp, mr, mg, mb, sha256,
       patch BLOB,                 -- 16×16 灰度指纹（256 字节）
       status, err, added)         -- ok / error / moved / deleted

roots(path UNIQUE, recursive, added, last_scan, status, err)

media_items(id, kind, path, root, name, size, mtime, dur, w, h,
            sig, aux, aux2, thumb, meta, status)   -- ok / skipped / error
media_roots(kind, path, recursive, added, status, total, done)
media_meta(k, v)
meta(k, v)                        -- 例如 hash_version
```

* `hash_version` 变化时（算法改动）会清空图片索引重建
* `patch` 是后加的列，`_migrate()` 自动 `ALTER TABLE`；老库缺的指纹**按需**补
* 存档导出**只取 `ARCHIVE_FIELDS`**（不含 patch / thumb 等 BLOB）：JSON 存不了 bytes，
  以前就是这里把"覆盖存档"写到一半搞崩的

---

## 七、接口约定

* 所有写操作都是 `POST` + JSON 对象；返回 `{"ok": true, ...}` 或 `{"ok": false, "error": ...}`
* 长任务：`POST /api/xxx/start` → 轮询 `GET /api/xxx/status`（`running/done/total/errors`）
* 二进制：`/api/thumb`、`/api/preview`（图片，带缓存）、`/api/media/thumb`、
  `/api/media/file`（**流式 + Range**，视频拖进度条靠它）
* 安全：`Origin` 与 `Host` 不一致的 POST 一律 403；请求体非 JSON 对象或 >256 MB → 400

---

## 八、性能与实测

| 项 | 数据 |
| --- | --- |
| 图片扫描 | 混合 237 / CPU 193 / GPU 135（张/秒，300 张真实照片） |
| 图片分组 | 6.8 万 3.5 s（换阈值 0.3 s）；50 万 4.7 s；100 万 20 s |
| 文本压缩 | 26.5 → 177 MB/s（16 线程，体积不变） |
| 解压 | 72 个包 86 MB：0.69 → 0.44 s（7-Zip 命令行 2.67 s） |
| 存档 | 6.8 万张 0.8 s / 30 MB |
| 破解 | AES zip 12 MH/s、ZipCrypto 40–120 MH/s、7z 1.57 MH/s、PDF R5 750k/s/core |

---

## 九、踩过的坑（回归测试盯着这些）

| 现象 | 根因 | 现在的做法 |
| --- | --- | --- |
| 完全不同的图片被分到一组 | 连通分量链式合并 + pHash 8~10 位段误判 | dHash 校验 + 灰度指纹 + 星形聚类 |
| 存档「覆盖」失败，目录留半截 `.tmp` | `SELECT *` 带出 `patch` BLOB，`json.dump` 抛错 | 只导存档字段；写失败清 `.tmp` |
| 视频页/音频页一打开就 500 | 上层把小说专用的 `content` 参数传给了 `group_video/group_audio` | 只给小说传；函数加 `**_kw` |
| 「其他文件」永远是 0 | `prune_missing` 把不在 `seen` 里的 skipped 行删了 | 跳过的路径也加入 `seen` |
| 没扫描就占 CPU | 启动时 8 线程补指纹 | 改成 `groups()` 首次调用时才补，4 线程 + 限速 + 侧栏进度 |
| 拖阈值滑块每次等 7 秒 | 候选检索每阈值重算 | 候选对带距离缓存（≤20），命中只做一次比较 |
| 点视频只看到一排文件名 | 服务端旧代码没有 `/api/media/file` | 新增流式接口；前端按扩展名选 video/img/audio |

回归测试在 `_test\test_*.py`（40 个），最快的跑法：

```
runtime\python.exe _test\test_media_chain.py
runtime\python.exe _test\test_archive.py
runtime\python.exe _test\test_patch_on_demand.py
runtime\python.exe _test\test_other_files.py
```

---

## 十、还能继续优化的地方

| 方向 | 想法 | 现状 |
| --- | --- | --- |
| 音频召回 | 增加"至少两个窗口匹配"约束，降低共用前奏的误判 | 未做（会需要重新标定阈值） |
| GPU 覆盖面 | DirectML 备用路径（`torch_directml`，选路 CUDA→DML→CPU） | **已做**；装 DML 后 AMD/Intel 也能走显卡，是否采纳由 auto 实测决定 |
| 分组增量 | 新图只跟已有组代表比，避免全量重算 | 未做（当前全量 3.5 s 尚可接受） |
| 视频特征 | 用更强的 embedding（CLIP 类）提升辨识度 | 未做（模型体积换精度） |
| 百万级内存 | 分桶矩阵按需分块，避免一次生成大矩阵 | 部分（已按宽高比分片） |
