# Blocking Shell

長時間のビルド・テストを1回のMCP呼び出しで終了まで待つCodex Plugin。
`omit_tools_from: ["code_mode"]` により直接呼び出しを使う。
サーバーは`systemd-run --user`のoneshotサービスの終了を非同期で待ち、
ポーリング用ハンドルを返さない。unitを保持して統計を取得してから停止・回収する。

必要環境はLinux、systemd 254以上の稼働中user manager、Bash、uv、Python 3.11以上。
呼び出し側にはuser busへの接続環境が必要。MCP SDKとtiktokenはuvが導入する。初回起動時にtiktokenが辞書をダウンロード・キャッシュする。
GitHubからインストールする（pluginコマンドに対応したCodexが必要）:

```sh
codex plugin marketplace add skmdx/blocking-shell
codex plugin add blocking-shell@blocking-shell
```

インストール後、新しいスレッドで利用する。
ソースを変更して試す場合は、cloneしたリポジトリの絶対パスを
`codex plugin marketplace add /absolute/path/to/blocking-shell` に指定する。
`.mcp.json`はインストール先を基準にサーバーを起動するため、固定の配置パスは不要。

`run` の引数・出力・実行権限は [スキル](plugins/blocking-shell/skills/blocking-shell/SKILL.md) と
ツールの説明を参照。ホスト側のツール期限は24時間より120秒長く設定し、
コマンドの期限超過時に終了処理と結果の返却を行う余裕を設けている。
共通引数は`exec_command`と同じ`cmd`、`workdir`、`max_output_tokens`、
`shell`、`login`、`tty`。MCPにはターンのcwdが渡されないため、`workdir`は必須。
`log_dir`も既存の絶対パスで指定する。独自引数`timeout_seconds`は既定21600秒。
通常のビルドでは省略する。指定すると既定値を上書きし、期限でコマンド自体を停止する。
結果の`timeout_seconds`で実際に適用した期限を確認できる。
全子孫プロセスを含むメモリ上限は既定8 GiB。`memory_max_mib`で正のMiB値を指定できる。
swapは禁止し、メモリ超過時はcgroup全体を停止する。上限を外した自動再試行はしない。
結果の`memory_max_bytes`と`memory_swap_max_bytes`に適用値を返す。
`yield_time_ms`とsandbox・承認引数は提供しない。
`shell`省略時はユーザーの既定シェル、`login=true`、`tty=false`。
`tty=true`で新しいPTYを割り当てる。標準入力への対話操作は提供しない。
`max_output_tokens`は既定10000。tiktokenの`o200k_base`で返却本文を実測し、
上限以内の末尾出力を返す。`output_tokens`に実トークン数、
`output_token_encoding`にエンコーディング名を返す。結果JSONのメタデータは対象外。
不正なUTF-8は置換してから計数する。利用モデルの課金トークン数を示すものではない。
完全なstdout/stderrは結合ログとして保存する。
環境ファイルの準備は不要。変数は`CC=clang make`のように`cmd`内で指定する。
転送する変数は`.mcp.json`の`env_vars`で指定し、別シェルでの変更は自動継承しない。
CPU時間・最大メモリ・ブロックIO量の範囲と単位はスキルを参照。
サーバーの強制終了やホスト障害からのジョブ復元は行わない。

`cleanup` を引数なしで直接呼ぶと、現在のMCPサーバーセッションで `run` が
作成した結果ディレクトリを、複数の `log_dir` をまたいで一括削除する。
実行中の結果は除外し、別セッションや無関係なファイルは削除しない。
返却値は `deleted`、`missing`、`skipped_active` のパス一覧と、パス別の `errors`。
削除失敗分は再試行できる。記録はメモリ上に保持するため、サーバー再起動前の
結果は対象外となり、手動で削除する。

実MCP通信の試験:

```sh
uv run --script plugins/blocking-shell/tests/test_mcp.py /absolute/scratch
```

インストール済みPluginを実Codexで確認する試験（Codex利用枠を使用）:

```sh
python3 plugins/blocking-shell/tests/smoke_codex.py --codex /path/to/codex --out-dir /absolute/new-scratch
```

65秒待機を含むMakefileでCプログラムをコンパイル・実行し、Codexの実記録で
直接呼び出し1回、途中のポーリング0回、正常終了、実行結果を検証する。
呼び出しで期限を上書きせず、既定21600秒が適用されることも確認する。
試験の出力ディレクトリは確認後に削除する。

MIT License。詳細は [LICENSE](LICENSE) を参照。
