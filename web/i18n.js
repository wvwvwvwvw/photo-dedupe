/* =========================================================================
   多语言界面（中文 / English / 日本語）
   -------------------------------------------------------------------------
   做法：以中文原文为 key 的"短语表"，直接替换页面上的文本节点和
   placeholder/title/alt 属性；动态新增的节点用 MutationObserver 增量翻译。
   * 没收录的短语保持中文（安全兜底，绝不影响功能）
   * 选择存在 localStorage['pd.lang']，默认 auto = 跟随系统语言
   * 数字类短语（"68,059 张图片"）用正则规则处理
   ========================================================================= */
(function () {
  "use strict";

  const EN = {
    // 品牌 / 导航
    "图片查重": "Photo Dedupe",
    "本机运行 · 不上传": "Local only · nothing uploaded",
    "查重": "Dedupe",
    "图片": "Photos",
    "视频": "Videos",
    "音频": "Audio",
    "小说": "Novels",
    "文本与压缩": "Text & Archive",
    "广告过滤": "Ad filter",
    "繁简 / 编码": "Chinese / Encoding",
    "打包": "Pack",
    "解压": "Unpack",
    "暴力破解": "Password crack",
    "设置": "Settings",
    "存档": "Archives",
    "补指纹…": "Fingerprints…",
    "引擎检测中…": "Detecting engine…",

    // 扫描区
    "开始扫描": "Start scan",
    "更新扫描": "Update scan",
    "暂停": "Pause",
    "继续": "Resume",
    "停止": "Stop",
    "准备中…": "Preparing…",
    "继续扫描": "Resume scan",
    "忽略": "Ignore",
    "上次有": "Last time",
    "个文件夹没扫完，已完成的图片不会重复分析。": "folder(s) unfinished — analysed photos won't be re-scanned.",

    // 添加文件夹
    "浏览": "Browse",
    "添加": "Add",
    "添加后自动扫描": "Scan after adding",
    "含子文件夹": "Include subfolders",
    "添加照片文件夹，例如 E:\\照片 或 D:\\DCIM": "Add a photo folder, e.g. E:\\Photos or D:\\DCIM",
    "添加文件夹，例如 D:\\视频": "Add a folder, e.g. D:\\Videos",
    "选择文件夹": "Choose folder",
    "选择照片文件夹": "Choose photo folder",

    // 统计 / 筛选
    "张图片": "photos",
    "组重复": "groups",
    "张多余": "extras",
    "可释放": "reclaimable",
    "相似度阈值": "Similarity threshold",
    "（哈希位数，越小越严）": "(hash bits, lower = stricter)",
    "排序": "Sort",
    "可释放空间": "Reclaimable space",
    "重复张数": "Duplicate count",
    "相似度": "Similarity",
    "分桶检索": "bucketed search",
    "评分": "score",
    "组内相似": "group similarity",
    "完全相同 ×": "Identical ×",
    "已经是最佳": "Already best",
    "全部已扫描": "All scanned",
    "索引顺序": "Index order",
    "每组至少": "Min per group",
    "范围": "Scope",
    "全部已扫描": "All scanned",
    "检索强度": "Recall",
    "快速": "Fast",
    "标准": "Standard",
    "彻底": "Thorough",
    "占用": "Load",
    "自动选择": "Auto-select",
    "其他文件": "Other files",
    "导出清单": "Export CSV",
    "画面相似度": "Frame similarity",
    "指纹重合度": "Fingerprint match",
    "正文分析": "Analyse text",
    "含视频里的音轨": "Include video audio",
    "全部已扫描": "All scanned",

    // 空状态
    "先添加一个照片文件夹": "Add a photo folder first",
    "可以陆续添加多个文件夹，扫描结果会累积在一起对比。": "You can add folders over time; results accumulate.",
    "中途可以暂停，关掉程序下次继续，已分析过的图片不会重复计算。": "Pause anytime — unfinished work continues next launch.",
    "支持 JPG / PNG / WebP / BMP / GIF / TIFF / AVIF 等": "Supports JPG / PNG / WebP / BMP / GIF / TIFF / AVIF …",
    "所有分析都在本机完成，图片不会被上传": "Everything runs locally — nothing is uploaded",
    "还没有扫描结果": "No scan results yet",
    "添加一个文件夹，然后点「开始扫描」": "Add a folder, then click “Start scan”",
    "显示更多": "Show more",

    // 组卡片
    "组": "Group",
    "建议保留": "Keep",
    "完全相同": "Identical",
    "含留边": "Has borders",
    "保留最佳": "Keep best",
    "反选": "Invert",
    "取消": "Cancel",
    "删除": "Delete",
    "保留": "Keep",
    "预览": "Preview",
    "对比": "Compare",
    "堆叠": "Stack",
    "叠加": "Overlay",
    "并排": "Side by side",
    "差异": "Difference",
    "打开所在文件夹": "Open folder",
    "设为此张保留": "Keep this one",
    "标记删除": "Mark for deletion",

    // 底部操作条
    "张已选": "selected",
    "项已选": "selected",
    "清空选择": "Clear",
    "移到文件夹": "Move to folder",
    "移到回收站": "Move to trash",

    // 打包 / 解压 / 转换
    "输入文件夹": "Input folder",
    "输出文件夹": "Output folder",
    "模式": "Mode",
    "单次压缩：整个输入打成一个包": "Single archive: pack the whole input",
    "批量压缩：每个文件/文件夹各打一个包": "Batch: one archive per file/folder",
    "压缩流程": "Pipeline",
    "按顺序执行，中间产物都在内存里，最后一步才写盘": "Runs in order; intermediates stay in memory",
    "压缩": "Compress",
    "合并再压一次": "Merge + compress",
    "拆分": "Split",
    "改输出名": "Rename output",
    "拍平嵌套": "Flatten",
    "+ 添加步骤": "+ Add step",
    "恢复默认": "Reset",
    "运行流程": "Run",
    "怎么做": "How",
    "繁体 → 简体": "Traditional → Simplified",
    "输出编码": "Output encoding",
    "换行": "Line endings",
    "保持原样": "Keep as is",
    "统一 CRLF": "Force CRLF",
    "统一 LF": "Force LF",
    "试一下": "Try it",
    "复制结果": "Copy result",
    "开始解压": "Start unpacking",
    "开始过滤": "Start filtering",
    "密码本": "Password list",
    "字典（可多选，和上面的密码本一起试）": "Dictionaries (multi-select)",
    "读取外部…": "Load file…",
    "下载…": "Download…",
    "已破解密码": "Cracked passwords",
    "载入到密码本": "Load into list",
    "还没有破解记录": "No passwords cracked yet",
    "解压日志会显示在这里": "Unpack log appears here",

    // 破解
    "目标": "Target",
    "选压缩包": "Pick archive",
    "怎么试": "How to try",
    "智能：先试密码本，再按位数穷举": "Smart: dictionary first, then brute force",
    "只用密码本（可自动变形）": "Dictionary only (with variations)",
    "只按数字穷举": "Digits only",
    "按字符集穷举（大小写/符号）": "Charset brute force",
    "变形：": "Variations:",
    "大小写": "Case",
    "字母换数字（a→@）": "Leet (a→@)",
    "末尾加数字": "Append digits",
    "位数": "Length",
    "字符集": "Charset",
    "长度": "Length",
    "前后缀": "Prefix / suffix",
    "开始破解": "Start cracking",
    "最多跑": "Max",
    "分钟": "min",
    "检测 GPU 中…": "Detecting GPU…",
    "高级：已知明文攻击（ZipCrypto 专用，不需要知道密码）": "Advanced: known-plaintext attack (ZipCrypto)",
    "压缩包": "Archive",
    "已知明文": "Known plaintext",
    "自动猜文件头（不用填明文，只对不压缩的加密条目有效）": "Guess header automatically",
    "或明文文件": "or plaintext file",
    "选文件": "Pick file",
    "偏移": "Offset",
    "顺便反推原密码": "Recover original password",
    "开始明文攻击": "Start attack",
    "破解日志（点开看细节）": "Crack log (click to expand)",
    "日志会显示在这里": "Log appears here",

    // 媒体预览
    "点一下波形可以跳转": "Click the waveform to seek",
    "文件读不到，可能已经被移动或删除": "Can't read the file — it may have been moved or deleted",
    "◀ 上一个": "◀ Prev",
    "下一个 ▶": "Next ▶",
    "系统播放器": "System player",
    "波形": "Waveform",
    "这些文件没有参与查重：体积太小、读不出来、后缀和真实内容对不上，或者只是动图。 仍然可以点「预览」看一眼，或「用系统程序打开」。":
      "These files were skipped: too small, unreadable, wrong extension, or just animated images. You can still preview them.",
    "系统程序": "Open with system app",
    "定位": "Reveal",

    // 存档
    "把这次的扫描结果存下来，下次直接接着用，不用重扫":
      "Save this scan so next time you can continue without re-scanning",
    "保存当前结果": "Save current result",
    "把现在索引里的记录写成一个存档文件": "Write the current index into an archive file",
    "我的存档": "My archives",
    "保存当前结果": "Save current result",
    "载入": "Load",
    "合并": "Merge",
    "覆盖": "Overwrite",
    "导出": "Export",
    "存档目录：": "Archive folder:",
    "校验失效文件": "Verify missing files",
    "导入存档文件…": "Import archive…",
    "给这次结果起个名字，例如：2024 家庭照片": "Name this result, e.g. Family photos 2024",

    // 设置
    "算力分配": "Compute",
    "自动（每次扫描时现场对比）": "Auto (benchmark on each scan)",
    "GPU 优先": "Prefer GPU",
    "CPU": "CPU",
    "占用越低越安静：CPU 线程数按比例减、显卡按比例间歇工作。扫描中途改设置，下次扫描生效。":
      "Lower load = quieter: fewer CPU threads, GPU works intermittently. Applies to the next scan.",
    "保存频率": "Flush frequency",
    "把改动存进设置（settings.json），重启后依然有效": "Saved to settings.json and kept after restart",
    "保存设置": "Save settings",
    "取消": "Cancel",

    // 杂项
    "确定": "OK",
    "关闭": "Close",
    "刷新": "Refresh",
    "换个文件夹": "Change folder",
    "全选": "Select all",
    "搜索": "Search",
    "加载中…": "Loading…",
    "读取中…": "Reading…",
    "处理中…": "Working…",
    "打开文件": "Open file",
    "打开": "Open",
    "关于": "About",
    "版本": "Version",
    "语言": "Language",
    "跟随系统": "System",
  };

  const JA = {
    "图片查重": "画像重複チェック",
    "本机运行 · 不上传": "ローカル実行 · アップロードなし",
    "查重": "重複チェック",
    "图片": "画像",
    "视频": "動画",
    "音频": "音声",
    "小说": "小説",
    "文本与压缩": "テキストと圧縮",
    "广告过滤": "広告除去",
    "繁简 / 编码": "繁簡・文字コード",
    "打包": "圧縮",
    "解压": "解凍",
    "暴力破解": "パスワード解析",
    "设置": "設定",
    "存档": "アーカイブ",
    "补指纹…": "指紋を補完…",
    "引擎检测中…": "エンジン検出中…",
    "开始扫描": "スキャン開始",
    "更新扫描": "スキャン更新",
    "暂停": "一時停止",
    "继续": "再開",
    "停止": "停止",
    "准备中…": "準備中…",
    "继续扫描": "スキャン再開",
    "忽略": "無視",
    "浏览": "参照",
    "添加": "追加",
    "添加后自动扫描": "追加後に自動スキャン",
    "含子文件夹": "サブフォルダを含む",
    "选择文件夹": "フォルダを選択",
    "张图片": "枚の画像",
    "组重复": "組の重複",
    "张多余": "枚の余分",
    "可释放": "解放可能",
    "相似度阈值": "類似度しきい値",
    "排序": "並び順",
    "可释放空间": "解放できる容量",
    "重复张数": "重複数",
    "相似度": "類似度",
    "分桶检索": "バケット検索",
    "评分": "スコア",
    "组内相似": "グループ類似度",
    "索引顺序": "インデックス順",
    "每组至少": "1組あたり最小",
    "范围": "範囲",
    "全部已扫描": "すべて",
    "检索强度": "検索強度",
    "快速": "高速",
    "标准": "標準",
    "彻底": "徹底",
    "占用": "使用率",
    "自动选择": "自動選択",
    "其他文件": "その他のファイル",
    "导出清单": "CSV出力",
    "画面相似度": "映像の類似度",
    "指纹重合度": "指紋の一致度",
    "正文分析": "本文を解析",
    "含视频里的音轨": "動画の音声も含める",
    "先添加一个照片文件夹": "まず写真フォルダを追加してください",
    "选择照片文件夹": "写真フォルダを選択",
    "还没有扫描结果": "スキャン結果がありません",
    "添加一个文件夹，然后点「开始扫描」": "フォルダを追加して「スキャン開始」を押してください",
    "显示更多": "もっと見る",
    "组": "組",
    "建议保留": "残す",
    "完全相同": "完全一致",
    "含留边": "余白あり",
    "保留最佳": "最良を残す",
    "反选": "反転",
    "取消": "キャンセル",
    "删除": "削除",
    "保留": "残す",
    "预览": "プレビュー",
    "对比": "比較",
    "叠加": "重ねる",
    "并排": "並べる",
    "差异": "差分",
    "打开所在文件夹": "フォルダを開く",
    "设为此张保留": "これを残す",
    "标记删除": "削除対象にする",
    "张已选": "枚選択",
    "项已选": "件選択",
    "清空选择": "選択解除",
    "移到文件夹": "フォルダへ移動",
    "移到回收站": "ゴミ箱へ移動",
    "输入文件夹": "入力フォルダ",
    "输出文件夹": "出力フォルダ",
    "模式": "モード",
    "压缩流程": "圧縮フロー",
    "压缩": "圧縮",
    "合并再压一次": "結合して再圧縮",
    "拆分": "分割",
    "改输出名": "出力名を変更",
    "拍平嵌套": "階層を平坦化",
    "+ 添加步骤": "+ ステップ追加",
    "恢复默认": "既定に戻す",
    "运行流程": "実行",
    "怎么做": "処理方法",
    "繁体 → 简体": "繁体字 → 簡体字",
    "输出编码": "出力エンコード",
    "换行": "改行コード",
    "保持原样": "そのまま",
    "试一下": "試す",
    "复制结果": "結果をコピー",
    "开始解压": "解凍開始",
    "开始过滤": "除去開始",
    "密码本": "パスワードリスト",
    "已破解密码": "解析済みパスワード",
    "载入到密码本": "リストに読み込む",
    "解压日志会显示在这里": "解凍ログがここに表示されます",
    "目标": "対象",
    "选压缩包": "書庫を選択",
    "怎么试": "試し方",
    "变形：": "変形：",
    "大小写": "大文字小文字",
    "末尾加数字": "末尾に数字",
    "位数": "桁数",
    "字符集": "文字種",
    "长度": "長さ",
    "前后缀": "前後の文字",
    "开始破解": "解析開始",
    "最多跑": "最大",
    "分钟": "分",
    "检测 GPU 中…": "GPU 検出中…",
    "压缩包": "書庫",
    "已知明文": "既知の平文",
    "选文件": "ファイル選択",
    "偏移": "オフセット",
    "开始明文攻击": "攻撃開始",
    "破解日志（点开看细节）": "解析ログ（クリックで詳細）",
    "日志会显示在这里": "ログがここに表示されます",
    "点一下波形可以跳转": "波形をクリックでシーク",
    "文件读不到，可能已经被移动或删除": "ファイルを読み込めません（移動・削除された可能性）",
    "◀ 上一个": "◀ 前へ",
    "下一个 ▶": "次へ ▶",
    "系统播放器": "システムプレーヤー",
    "系统程序": "既定のアプリで開く",
    "定位": "場所を開く",
    "把这次的扫描结果存下来，下次直接接着用，不用重扫": "今回のスキャン結果を保存して次回に続きから使えます",
    "保存当前结果": "現在の結果を保存",
    "我的存档": "マイアーカイブ",
    "载入": "読み込み",
    "合并": "統合",
    "覆盖": "上書き",
    "导出": "エクスポート",
    "存档目录：": "保存先：",
    "校验失效文件": "欠損ファイルを検証",
    "导入存档文件…": "アーカイブを読み込む…",
    "算力分配": "演算リソース",
    "自动（每次扫描时现场对比）": "自動（スキャン時に比較）",
    "GPU 优先": "GPU 優先",
    "保存频率": "保存頻度",
    "保存设置": "設定を保存",
    "确定": "OK",
    "关闭": "閉じる",
    "语言": "言語",
    "跟随系统": "システム設定",
  };

  // 动态短语（带数字/文件名）用规则处理
  const RULES = {
    en: [
      [/^(\d[\d,]*) 张图片$/, "$1 photos"],
      [/^(\d[\d,]*) 组重复$/, "$1 groups"],
      [/^(\d[\d,]*) 张多余$/, "$1 extras"],
      [/^(\d[\d,]*) 项已选$/, "$1 selected"],
      [/^(\d[\d,]*) 张已选$/, "$1 selected"],
      [/^组 (\d+)$/, "Group $1"],
      [/^(\d+) 张$/, "$1 items"],
      [/^(\d+) 个$/, "$1 items"],
      [/^(\d+) 个文件$/, "$1 files"],
      [/^(\d+) 视频$/, "$1 videos"],
      [/^(\d+) 音频$/, "$1 audio"],
      [/^(\d+) 小说$/, "$1 novels"],
      [/^(.+) 组 · 相似 (.+)–(.+)$/, "$1 · similarity $2–$3"],
      [/^可释放 ([\d.]+ .+)$/, "Reclaim $1"],
      [/^已载入$/, "Loaded"],
      [/^还有 (\d+) 个$/, "$1 more"],
      [/^相似度 (.+)$/, "Similarity $1"],
      [/^相似 ([\d.]+)%$/, "Similar $1%"],
      [/^组内相似 ([\d.]+)%$/, "group similarity $1%"],
      [/^完全相同 ×(\d+)$/, "Identical ×$1"],
      [/^可释放 (.+)$/, "Reclaim $1"],
      [/^(.+) · 评分 ([\d.]+)$/, "$1 · score $2"],
      [/^(.+) · 分桶检索$/, "$1 · bucketed search"],
      [/^(\d+) 组$/, "$1 groups"],
      [/^(\d+) 项$/, "$1 items"],
      [/^(\d+) 张$/, "$1 photos"],
      [/^(\d+) 个文件夹$/, "$1 folders"],
    ],
    ja: [
      [/^(\d[\d,]*) 张图片$/, "$1 枚の画像"],
      [/^(\d[\d,]*) 组重复$/, "$1 組"],
      [/^(\d[\d,]*) 张多余$/, "$1 枚の余分"],
      [/^(\d[\d,]*) 项已选$/, "$1 件選択"],
      [/^(\d[\d,]*) 张已选$/, "$1 枚選択"],
      [/^组 (\d+)$/, "組 $1"],
      [/^还有 (\d+) 个$/, "他 $1 件"],
      [/^相似度 (.+)$/, "類似度 $1"],
      [/^相似 ([\d.]+)%$/, "類似 $1%"],
      [/^组内相似 ([\d.]+)%$/, "グループ類似度 $1%"],
      [/^完全相同 ×(\d+)$/, "完全一致 ×$1"],
      [/^(.+) · 评分 ([\d.]+)$/, "$1 · スコア $2"],
      [/^(.+) · 分桶检索$/, "$1 · バケット検索"],
      [/^(\d+) 组$/, "$1 組"],
    ],
  };

  // 整句匹配不到时，兜底替换这类"夹在长句里的单位词"
  const INLINE = {
    en: [["评分", "score"], ["相似", "similar"], ["张图片", "photos"], ["组重复", "groups"]],
    ja: [["评分", "スコア"], ["相似", "類似"], ["张图片", "枚の画像"], ["组重复", "組"]],
  };

  const ATTRS = ["placeholder", "title", "alt", "aria-label"];
  const SRC_TEXT = new WeakMap();
  const SRC_ATTR = new WeakMap();
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA"]);

  const I18N = {
    lang: "zh",
    pref: "auto",
    dict: {},
    rules: [],
  };

  function detect(pref) {
    if (pref && pref !== "auto") return pref;
    const nav = (navigator.language || navigator.userLanguage || "zh").toLowerCase();
    if (nav.startsWith("zh")) return "zh";
    if (nav.startsWith("ja")) return "ja";
    return "en";
  }

  function translatePhrase(raw) {
    const s = raw.trim();
    if (!s) return null;
    const d = I18N.dict;
    if (Object.prototype.hasOwnProperty.call(d, s)) return d[s];
    for (const [re, rep] of I18N.rules) {
      if (re.test(s)) return s.replace(re, rep);
    }
    return null;
  }

  function translateTextNode(node) {
    const cur = node.nodeValue;
    if (!cur) return;
    const known = SRC_TEXT.has(node);          // 之前翻译过（原文已记下）
    if (!known && !/[\u4e00-\u9fff]/.test(cur)) return;   // 没中文、也没记录 → 不用管
    if (!known) SRC_TEXT.set(node, cur);
    const original = SRC_TEXT.get(node);
    if (I18N.lang === "zh" || !/[\u4e00-\u9fff]/.test(original)) {
      if (node.nodeValue !== original) node.nodeValue = original;
      return;
    }
    let tr = translatePhrase(original);
    if (tr == null) {
      // 整句没命中：把里面的单位词换掉（只动中文词，不碰数字和符号）
      let txt = original;
      const pairs = INLINE[I18N.lang] || [];
      for (const [zh, other] of pairs) {
        if (txt.includes(zh)) txt = txt.split(zh).join(other);
      }
      tr = txt !== original ? txt : null;
    }
    const m = /^(\s*)([\s\S]*?)(\s*)$/.exec(original) || [];
    const next = tr == null ? original : (m[1] || "") + tr.trim() + (m[3] || "");
    if (node.nodeValue !== next) node.nodeValue = next;
  }

  function translateAttrs(el) {
    if (!el || !el.getAttribute) return;
    let store = SRC_ATTR.get(el);
    if (!store) { store = {}; SRC_ATTR.set(el, store); }
    for (const a of ATTRS) {
      const v = el.getAttribute(a);
      if (v == null) continue;
      // 第一次见到的值记成原文；之后一律以原文为基准
      if (!(a in store)) {
        if (!/[\u4e00-\u9fff]/.test(v)) continue;   // 本来就是外文，不用管
        store[a] = v;
      }
      const orig = store[a];
      if (I18N.lang === "zh") {
        if (v !== orig) el.setAttribute(a, orig);
        continue;
      }
      if (!/[\u4e00-\u9fff]/.test(orig)) continue;
      const tr = translatePhrase(orig);
      if (tr != null && v !== tr) el.setAttribute(a, tr);
    }
  }

  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) { translateTextNode(root); return; }
    if (root.nodeType !== 1) return;
    if (SKIP.has(root.tagName)) return;
    translateAttrs(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let n;
    while ((n = walker.nextNode())) {
      if (n.parentNode && SKIP.has(n.parentNode.tagName)) continue;
      translateTextNode(n);
    }
    if (root.querySelectorAll) {
      root.querySelectorAll("*").forEach((el) => translateAttrs(el));
    }
  }

  I18N.setLang = function (pref) {
    I18N.pref = pref || "auto";
    I18N.lang = detect(I18N.pref);
    I18N.dict = I18N.lang === "en" ? EN : (I18N.lang === "ja" ? JA : {});
    I18N.rules = RULES[I18N.lang] || [];
    try { localStorage.setItem("pd.lang", I18N.pref); } catch (_) {}
    document.documentElement.lang = I18N.lang === "zh" ? "zh-CN" : I18N.lang;
    walk(document.body);
    // 语言切换后重新渲染一遍动态区域（数字/单位会跟着变）
    window.dispatchEvent(new CustomEvent("pd:lang", { detail: { lang: I18N.lang } }));
  };

  I18N.t = function (s) {
    if (I18N.lang === "zh") return s;
    const tr = translatePhrase(s);
    return tr == null ? s : tr;
  };

  I18N.apply = function (root) { walk(root || document.body); };

  // 动态内容：批量翻译（节流）
  let pending = false;
  const obs = new MutationObserver((muts) => {
    if (I18N.lang === "zh") return;
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      for (const m of muts) {
        m.addedNodes.forEach((n) => { if (n.nodeType === 1 || n.nodeType === 3) walk(n); });
        if (m.type === "characterData") translateTextNode(m.target);
      }
    });
  });

  I18N.start = function () {
    let pref = "auto";
    try { pref = localStorage.getItem("pd.lang") || "auto"; } catch (_) {}
    I18N.setLang(pref);
    obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    const sel = document.getElementById("langSel");
    if (sel) {
      sel.value = I18N.pref;
      sel.onchange = () => {
        I18N.setLang(sel.value);
        try { localStorage.setItem("pd.lang", sel.value); } catch (_) {}
      };
    }
  };

  window.I18N = I18N;
})();
