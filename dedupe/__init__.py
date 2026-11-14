"""图片查重核心模块：格式、哈希、引擎、索引、聚类、索引库、扫描、存档。

模块一览：
  formats   格式探测（Pillow 注册表 + 可选插件）
  hashing   CPU 分析（Pillow，JPEG 走 draft 快速解码）
  gpupipe   GPU 分析（nvJPEG + 显存内计算）
  pipeline  引擎选择、GPU/CPU 一致性校验、基准测试
  index     分桶检索（LSH）与连通分量
  grouping  聚类编排与分页出组
  store     SQLite 索引 + numpy 索引装载
  archive   存档保存/载入/合并/导入导出
  gpu       向量化汉明距离内核
"""

__all__ = ["formats", "hashing", "gpupipe", "pipeline", "index",
           "grouping", "store", "scanner", "archive", "gpu"]
