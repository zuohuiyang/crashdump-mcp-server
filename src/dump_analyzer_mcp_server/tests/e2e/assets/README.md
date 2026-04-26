# E2E 测试资产说明

本目录不提交大体积 dump / PDB 文件，仅记录约定：

- 核心闭环默认使用仓库自带小 dump：`src/dump_analyzer_mcp_server/tests/dumps/DemoCrash1.exe.7088.dmp`
- 大 PDB 长时间加载场景使用外部资产，通过环境变量 `DUMP_E2E_SYMBOL_HEAVY_DUMP_PATH` 传入
- 建议在资产说明中记录目标应用版本、dump 采集方式（例如 `procdump -ma`）以及采集时间
