# Blocking Shell

会話ごとのBashを使い回し、各コマンドの完了まで1回のMCP呼び出しで待つCodex Plugin。
変数・export・関数・alias・作業ディレクトリを保持するため、初期化コードの再送が不要になる。

## 導入

Linux、systemd 254以上の稼働中user manager、Bash、uv、Python 3.11以上、
Plugin対応のCodexが必要。user busへ接続できること。
MCP SDK、tiktoken、tree-sitterはuvが導入する。

```sh
codex plugin marketplace add skmdx/blocking-shell
codex plugin add blocking-shell@blocking-shell
```

ローカル版はmarketplaceの追加先をclone先にする。更新後はMCPを再接続する。
フックの登録は不要。ログの整理にはScratchのdeleteを使える。

## 使い方

通常は`run(cmd=...)`だけでよい。

```text
run(cmd="export MODE=debug; n=0; step(){ n=$((n+1)); echo \"$MODE:$n\"; }")
run(cmd="step")    → debug:1
run(cmd="step")    → debug:2
```

`source ./sdk-env.sh`や`cd`も一度実行すれば次の呼び出しへ引き継ぐ。
`declare -p 名前`、`declare -f 関数名`、`compgen -v`などで状態を確認できる。
aliasを使う場合は`shopt -s expand_aliases`を設定する。
同じ会話の呼び出しは直列実行し、別の会話とは分離する。PTY・対話入力は提供しない。

| 操作 | 用途 |
| --- | --- |
| `run(cmd=...)` | 現在のBashで実行し、終了結果を返す |
| `reset()` | 子孫プロセスを停止してBashを作り直す。履歴・ログは残る |
| `result(run_ref=...)` | 保存結果を取得する。省略時は最後に受け付けた実行 |
| `rerun(run_ref=...)` | 保存したコマンドと条件を現在のBash状態で再実行する |

`run`の`workdir`は実行前の明示的なcd。`scratch_ref`はログ保存先の指定で、
省略すると自動作成した保存先を再利用する。選択先は毎回`SCRATCH_DIR`としてexportする。
`workdir="$SCRATCH_DIR"`とその配下も指定できる。結果の`scratch_ref`はScratchへ渡せる。
保存先が不要になったら削除する。実行中はleaseにより削除を防ぐ。

初期作業ディレクトリと一時保存先のルートは、サーバー起動時に存在しなければ作成する。
保存先のルートは`SCRATCH_ROOT`、未指定なら初期作業ディレクトリ下の`tmp`。
Bashの内部通信領域もこのルートを使い、`SCRATCH_ROOT`指定時は既定の`tmp`を必要としない。
明示的な`run`・`reset`の`workdir`は従来どおり既存ディレクトリを指定する。

| 設定 | 既定値・動作 |
| --- | --- |
| 初期作業ディレクトリ | `CODEX_WORK`、未指定なら`~/codex-work` |
| `timeout_seconds` | 呼び出しごとに6時間。1〜86400秒 |
| `memory_max_mib` | Bashと子孫全体で初期8 GiB。変更後は保持。swapはホスト設定に従う |
| `max_output_tokens` | ログ末尾1000トークン。メタデータは含まない |
| `reset(login=false)` | login startupを省略。既定はtrue。BASH_ENVはいずれでも読む |

## 状態と結果

通常の非ゼロ終了では状態を保持する。`exit`・`exec`・`set -e`によるシェル終了、
タイムアウト、キャンセル、OOMでは`state_lost`を返し、再開には`reset`が必要。
Bashの起動設定を変更する場合もresetを使う。省略したreset設定は現在のcwd・login・メモリ制限を保持する。
MCPサーバーの再起動はBash状態を失う。会話の圧縮だけなら、サーバーが存続している限り保持する。
削除済みのcwdからresetする場合は既存の`workdir`を指定する。

`status`と`exit_code`を確認する。`state=finished`だけでは成功を意味しない。
完全ログと詳細JSONは`log_path`・`result_path`に保存する。
CPU・IOは実行区間のcgroupカウンタ差分で、並行するバックグラウンド処理も含む。
`session_memory_peak_bytes`はBash起動からの累積ピーク。取得不能はnullであり、ゼロではない。
`accounting_error`・`cleanup_error`があれば確認する。
出力制限はo200k_baseによる計数で、課金トークン数ではない。

バックグラウンドジョブは次の呼び出しへ残り、resetまたはサーバーの正常終了で停止する。
完了を評価する作業はフォアグラウンドで実行する。
タイムアウトには独立したsystemd timerも設定し、サーバーの強制終了後も停止期限を守る。
強制終了時の結果確定やBash状態の復元は行わない。

通信失敗時は再実行する前にresultを使う。確定済みならrunと同じ要約を返す。
未確定はrunning、所有者も結果も失われた場合はunknown、ログ削除済みはexpired。
履歴はサーバー再起動後も残る。rerunは副作用を繰り返すため、通常のrunと同じ実行許可が必要。

共通の環境設定はCodexユーザー設定の`shell_environment_policy.set.BASH_ENV`を参照し、
未指定時はサーバー起動時のBASH_ENVを継承する。Bashの起動時に一度だけ読む。
従来のset_env・bashrc登録ツールと圧縮フックは削除した。export・unset・sourceを直接使う。
旧登録内容は自動適用しない。履歴DBは従来と同じ
`$XDG_STATE_HOME/blocking-shell`（既定`~/.local/state/blocking-shell`）に置き、
`BLOCKING_SHELL_STATE_DIR`で変更できる。

内部通信にはFD 197・198と`__blocking_shell_`で始まる名前を予約する。
これらを変更しない。コードはBashのトップレベルでsourceされ、関数やサブシェルでは包まない。

長い類似コマンドが同じ会話・cwdで繰り返される場合は、任意のsuggestionで共通化を提案する。
400文字以上・32 KiB以下のBash/Python本文を対象に最近64候補を保持し、3回目に一度だけ通知する。
コマンドの書換えや自動実行は行わない。

## 開発時の検証

TMPDIRを既存の一時保存先に設定して実行する。実MCP通信とsystemdを使用する。

```sh
uv run --script plugins/blocking-shell/tests/test_startup.py
uv run --script plugins/blocking-shell/tests/test_repl.py
uv run --script plugins/blocking-shell/tests/test_recovery.py
uv run --script plugins/blocking-shell/tests/test_history.py
uv run --script plugins/blocking-shell/tests/test_patterns.py
uv run --script plugins/blocking-shell/tests/test_suggestions.py
```

`tests/smoke_codex.py`は明示的な実行時のみCodex利用枠を使い、実Codexでビルドと直接呼び出しを確認する。
`experiments/template_similarity.py`は取得済みの履歴に対するオフライン分析であり、コマンドは実行しない。

エージェント向けの手順は[スキル](plugins/blocking-shell/skills/blocking-shell/SKILL.md)、
未解決の制約は[TODO](TODO.md)を参照。MIT License。
