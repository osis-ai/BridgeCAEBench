# scoring

评分代码，以本仓库为准，不依赖 `bench`，官方仓库可以直接引用。

- `evaluation/`：复制自官方仓库 `src/evaluation/`（commit `37f7685`），配置在 `configs/evaluation.yaml`。
  复制后用 9 个历史 modeling 运行核对，总分和各分项与官方一致。之后的修改只在这里做。
