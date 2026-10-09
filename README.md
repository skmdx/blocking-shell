# Blocking Shell

長時間のビルド・テストを1回のMCP呼び出しで終了まで待つCodex Plugin。
終了状態、ログ末尾、子孫プロセスを含むCPU・メモリ・IO統計を返す。
PTY・対話入力・ポーリング用ハンドルは提供しない。

## 導入

Linux、systemd 254以上の稼働中user manager、Bash、uv、Python 3.11以上、
pluginコマンドに対応したCodexが必要。呼び出し側からuser busへ接続できること。
MCP SDKとtiktokenはuvが導入し、初回起動時にtiktokenの辞書をダウンロードする。
実行結果の保存にはScratchプラグインも必要。

```sh
codex plugin marketplace add skmdx/blocking-shell
codex plugin add blocking-shell@blocking-shell
```

インストール後は新しいスレッドで利用し、`/hooks`でフックを確認・信頼する。
ローカルのソースを試す場合は、marketplaceの追加先をcloneしたリポジトリの絶対パスにする。

## 実行と結果の確認

1. Scratchの`create`で保存先を作成する。
2. `run`に`cmd`、既存ディレクトリの`workdir`、作成した`scratch_ref`を渡し、直接呼び出す。
3. `status`・`exit_code`とログ末尾を確認する。不足する場合だけ`log_path`の完全ログや`result_path`の詳細JSONを読む。
4. 結果が不要になったらScratchの`delete`で保存先を削除する。実行中の保存先は削除されない。

`workdir`には絶対パスのほか、`$SCRATCH_DIR`とその配下を指定できる。
コマンド内でも`"$SCRATCH_DIR"`で選択した保存先を参照できる。

`rerun()`は同じ会話で直前に受け付けたコマンドを、現在の環境設定で再実行する。
実行条件は引き継ぎ、ログは新規作成する。保存先を変更する場合は
`rerun(scratch_ref="...")`を使う。履歴はMCP再接続・圧縮後も維持される。
通信失敗時は`result()`で最後に受け付けた実行を確認してから再実行を判断する。
既知の実行には`run`・`rerun`が返した`run_ref`を渡せる。同時実行時の「最後」は受付順。
`state=finished`なら通常実行と同じ要約を返す。それ以外は`running`・`finishing`・
`unknown`（結果未確定）・`expired`（保存先削除済み）を区別し、成功を推測しない。
この対応記録は更新後の実行から作成される。過去のログを自動探索・登録はしない。

| 設定 | 既定値・動作 |
| --- | --- |
| `timeout_seconds` | 6時間。1〜86400秒で変更でき、期限超過時は子孫プロセスも停止する |
| `memory_max_mib` | 子孫全体で8 GiB。swapは禁止し、超過時はcgroup全体を停止する |
| `max_output_tokens` | ログ末尾1000トークン。結果メタデータは含まない |
| `shell`・`login` | ユーザーの既定シェルで`-lc`。`login=false`なら`-c` |

統計はunit cgroupの回収前に取得し、取得不能は`null`で表す。
`accounting_error`・`cleanup_error`がある場合は、計測や後片付けの失敗を確認する。
ログのトークン数は`o200k_base`による計数で、利用モデルの課金トークン数ではない。
サーバーの強制終了やホスト障害からのジョブ復元は行わない。

各ツールの操作契約と実行時の注意点は[スキル](plugins/blocking-shell/skills/blocking-shell/SKILL.md)を参照。

## 環境設定

設定を使う期間と、値だけかBash処理が必要かで選ぶ。

| 局面 | 選ぶ方法 | 例 |
| --- | --- | --- |
| 次の1回だけコンパイラや初期化を変える | `run`の`cmd`に書く | `CC=clang make`、`source ./sdk-env.sh && make` |
| この会話でビルドとテストに同じ値を渡す | `set_env` | `set_env(values={"CC": "clang"})` |
| この会話で関数・alias・SDK初期化を繰り返し使う | `set_bashrc` | `set_bashrc(script="source ./sdk-env.sh")` |
| 別の会話やツールでも常に使う | 共通の`BASH_ENV`ファイル | 普段使う関数や環境変数 |

`set_bashrc`はBash本文を登録する。たとえば`source ./sdk-env.sh`は各コマンドの
`workdir`を基準に、実行のたびに読み込まれる。作業ディレクトリを変える場合は、
共通して参照できるパスを指定する。コンパイルなどの本処理は`cmd`に置く。
値だけの設定は`set_env`で十分で、事前の設定が不要ならそのまま`run`を使う。

常設のalias・関数・環境変数は、Codexのユーザー設定
`$CODEX_HOME/config.toml`（既定`~/.codex/config.toml`）の
`[shell_environment_policy.set]`に指定した`BASH_ENV`のファイルへまとめる。
`run`・`rerun`ごとにこの設定を読み、未指定なら起動元の`BASH_ENV`を継承する。
プロジェクト設定・profile・CLIの上書きは参照しない。Bashは`login`の値によらずこのファイルを読む。

環境変数は`list_env()`で確認し、`unset_env(names=["CC"])`で解除する。
Bash本文は登録時に返る`ref`を使い、`set_bashrc(ref="...", script="...")`で置換、
`delete_bashrc(ref="...")`で解除する。IDを忘れた場合は`list_bashrc()`、
本文の確認には`get_bashrc(refs=[...])`を使う。会話内での用途が終わった設定は解除する。

環境変数の更新応答は変更した名前を返し、全値は`list_env`で確認する。
Bash本文は対象別に成功・失敗を返す。`next_cursor`があれば同じ`refs`で続きを取得する。
途中で対象の本文が変わった場合は継続を拒否するため、先頭から読み直す。

会話の設定はMCP再接続・圧縮後も維持され、以後の`run`・`rerun`へ適用する。
他の会話・他のツール・実行中のコマンドには適用しない。
`set_env`は継承環境とユーザー設定の`BASH_ENV`より優先するが、シェルの起動処理で変更される場合がある。
登録したBash処理は通常の起動処理後、コマンド前に登録順で読み込む。
非ゼロで終了した場合は後続の処理を停止する。登録中はBashを使い、対話待ちは入れない。

状態の保存先は`$XDG_STATE_HOME/blocking-shell`（既定`~/.local/state/blocking-shell`）。
ホスト環境の`BLOCKING_SHELL_STATE_DIR`で変更できる。
自動圧縮後は`SessionStart`フックが環境変数の名前、Bash処理のIDをモデルへ伝える。
保存された値は引き続きコマンドへ適用され、通常実行のために再取得する必要はない。
Codex 0.162.0の手動圧縮APIではこのフックは発火しない。

## 開発時の検証

実MCP通信の試験は、一時保存先を指定して実行する。

```sh
uv run --script plugins/blocking-shell/tests/test_mcp.py /absolute/scratch
TMPDIR=/absolute/scratch uv run --script plugins/blocking-shell/tests/test_environment.py
TMPDIR=/absolute/scratch uv run --script plugins/blocking-shell/tests/test_rerun.py
TMPDIR=/absolute/scratch uv run --script plugins/blocking-shell/tests/test_recovery.py
```

インストール済みPluginを実Codexで確認する試験は、Codex利用枠を使用する。

```sh
python3 plugins/blocking-shell/tests/smoke_codex.py --codex /path/to/codex --out-dir /absolute/new-scratch
```

65秒待機を含むCプログラムのビルド・実行で、直接呼び出し1回、ポーリング0回、
正常終了、実行結果、既定の6時間期限を確認する。試験結果は確認後に削除する。

### テンプレート候補の実験

[template_similarity.py](experiments/template_similarity.py)はsession-historyの取得JSONを使い、
直近100回の`run`を最大50件前まで比較するオフラインPoC。
履歴のコマンドは実行せず、サーバーやインストール済みPluginも変更しない。
資源制限付きのblocking-shellから実行する。

```sh
uv run --script experiments/template_similarity.py history.json --out result.json
```

raw DEFLATEの対称化NCDとシェルトークン列のSequenceMatcherを比較し、前半で閾値を選び、後半で評価する。
評価は「pytest呼び出しが出力先だけ異なり、実行条件も一致する」という限定した基準。
完全一致は精度評価から除外し、長すぎる入力は除外数を返す。意味の類似性や別セッションへの汎化精度は測らない。

候補が3件以上あり、異なる`--option=/path`を分離できる場合に、完全復元を確認したテンプレート案を生成する。
レビュー用の案であり、安全なシェル生成・実行機能ではない。
トークン比較は`o200k_base`・空白なしJSONで定義と説明を1回含めた回顧的試算。
ツールスキーマ・応答・会話履歴の再入力を含まず、実現済みの節約量ではない。
結果JSONにはコマンドと実行条件を含むため、公開前に内容を確認する。
同じ選択上限で再入力すれば、全会話を保持せずに再評価できる。

MIT License。詳細は[LICENSE](LICENSE)を参照。
