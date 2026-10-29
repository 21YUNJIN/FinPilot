import { useId, useState, type FormEvent, type ReactElement } from "react";
import type { ApiFailure } from "../api/client";
import type { PrivacyExportView, RetentionRule } from "../types";
import { formatDateTime } from "../format/datetime";
import { ErrorBanner } from "./ErrorBanner";
import { cx } from "../lib/cx";

/**
 * 隐私面板（SEC-04 / A05）。
 *
 * 规则：
 * - 提供**导出**与**删除**两个真实入口（调用 /v1/privacy/export 与 /v1/privacy/delete）；
 * - 删除必须二次确认（输入"删除"），并说明不会直接抹除金融事实：交易/授权记录按监管留存要求保留，
 *   但不用于展示与自动化；删除由服务端按角色授权执行；
 * - 必须写明留存说明与本机会话缓存隔离：切换账号会清除本机缓存，不共享上一个用户的数据；
 * - 失败时显示服务端真实原因，不假装删除成功。
 */
export interface PrivacyPanelProps {
  retention?: RetentionRule[];
  exportView?: PrivacyExportView | null;
  exportBusy?: boolean;
  deleteBusy?: boolean;
  exportError?: ApiFailure | null;
  deleteError?: ApiFailure | null;
  deleteScopes?: Array<{ value: string; label: string; hint?: string }>;
  onExport?: () => void;
  onDeleteRequest?: (input: { scope: string; reason: string }) => void;
  className?: string;
  "data-testid"?: string;
}

const DEFAULT_SCOPES: Array<{ value: string; label: string; hint?: string }> = [
  { value: "conversation", label: "对话与任务上下文", hint: "删除后不再用于自动化，但审计留痕保留。" },
  { value: "preferences", label: "偏好与目标摘要", hint: "删除后会重新询问，不影响既有资金事实。" },
  { value: "all", label: "以上全部（不含金融事实）", hint: "金融交易与授权记录按监管要求保留。" },
];

const CONFIRM_WORD = "删除";

export function PrivacyPanel({
  retention,
  exportView,
  exportBusy = false,
  deleteBusy = false,
  exportError,
  deleteError,
  deleteScopes = DEFAULT_SCOPES,
  onExport,
  onDeleteRequest,
  className,
  "data-testid": dataTestId = "privacy-panel",
}: PrivacyPanelProps): ReactElement {
  const [scope, setScope] = useState(deleteScopes[0]?.value ?? "conversation");
  const [reason, setReason] = useState("");
  const [confirmText, setConfirmText] = useState("");
  const scopeId = useId();
  const reasonId = useId();
  const confirmId = useId();

  const confirmed = confirmText.trim() === CONFIRM_WORD;

  function handleDelete(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!confirmed || deleteBusy) return;
    onDeleteRequest?.({ scope, reason: reason.trim() });
    setConfirmText("");
  }

  return (
    <section className={cx("fp-card", "fp-privacy", className)} aria-label="隐私与数据" data-testid={dataTestId}>
      <header className="fp-card__header">
        <h2 className="fp-card__title">隐私与数据</h2>
      </header>

      <section className="fp-privacy__section" aria-labelledby="privacy-export">
        <h3 id="privacy-export">导出我的数据</h3>
        <p className="fp-privacy__hint">
          导出由服务端生成（含任务、授权与操作记录，不含验证码与密码）；本页面只展示服务端返回的凭据与有效期，
          不在前端拼装隐私数据。
        </p>
        <div className="fp-privacy__actions">
          <button
            type="button"
            className="fp-button fp-button--primary"
            onClick={onExport}
            disabled={exportBusy || !onExport}
            aria-busy={exportBusy}
            data-testid="privacy-export-button"
          >
            {exportBusy ? "正在生成导出…" : "导出我的数据"}
          </button>
        </div>
        {exportError ? <ErrorBanner failure={exportError} context="数据导出" showCode /> : null}
        {exportView ? (
          <dl className="fp-privacy__export" data-testid="privacy-export-result">
            <div>
              <dt>导出编号</dt>
              <dd>{exportView.request_id ?? "未提供"}</dd>
            </div>
            <div>
              <dt>生成时间</dt>
              <dd>{formatDateTime(exportView.generated_at, "未提供")}</dd>
            </div>
            <div>
              <dt>下载凭据</dt>
              <dd>{exportView.download_url ?? "不适用（服务端未返回下载地址）"}</dd>
            </div>
            <div>
              <dt>过期时间</dt>
              <dd>{formatDateTime(exportView.expires_at, "不适用")}</dd>
            </div>
            {exportView.note ? (
              <div>
                <dt>说明</dt>
                <dd>{exportView.note}</dd>
              </div>
            ) : null}
          </dl>
        ) : null}
      </section>

      <section className="fp-privacy__section" aria-labelledby="privacy-delete">
        <h3 id="privacy-delete">请求删除</h3>
        <p className="fp-privacy__hint">
          删除请求由服务端按角色授权执行。为满足监管与对账要求，交易、授权与审计记录属于「金融事实」，
          不会被直接抹除：它们将被标记为不再用于展示与自动化，仅保留法定留存期。
        </p>
        <form className="fp-privacy__form" onSubmit={handleDelete}>
          <label className="fp-privacy__label" htmlFor={scopeId}>
            删除范围
          </label>
          <select
            id={scopeId}
            className="fp-privacy__select"
            value={scope}
            onChange={(event) => setScope(event.target.value)}
            data-testid="privacy-delete-scope"
          >
            {deleteScopes.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          {deleteScopes.find((item) => item.value === scope)?.hint ? (
            <p className="fp-privacy__hint">{deleteScopes.find((item) => item.value === scope)?.hint}</p>
          ) : null}

          <label className="fp-privacy__label" htmlFor={reasonId}>
            原因（可选）
          </label>
          <input
            id={reasonId}
            className="fp-privacy__input"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={200}
            placeholder="例如：不再使用该服务"
          />

          <label className="fp-privacy__label" htmlFor={confirmId}>
            输入"{CONFIRM_WORD}"以确认（防止误触）
          </label>
          <input
            id={confirmId}
            className="fp-privacy__input"
            value={confirmText}
            onChange={(event) => setConfirmText(event.target.value)}
            data-testid="privacy-delete-confirm"
          />

          <div className="fp-privacy__actions">
            <button
              type="submit"
              className="fp-button fp-button--danger"
              disabled={!confirmed || deleteBusy || !onDeleteRequest}
              aria-disabled={!confirmed || deleteBusy || !onDeleteRequest}
              data-testid="privacy-delete-button"
            >
              {deleteBusy ? "正在提交删除请求…" : "提交删除请求"}
            </button>
          </div>
        </form>
        {deleteError ? <ErrorBanner failure={deleteError} context="删除请求" showCode /> : null}
      </section>

      <section className="fp-privacy__section" aria-labelledby="privacy-retention">
        <h3 id="privacy-retention">留存说明</h3>
        {retention && retention.length > 0 ? (
          <table className="fp-table" data-testid="privacy-retention-table">
            <thead>
              <tr>
                <th scope="col">数据类别</th>
                <th scope="col">留存期</th>
                <th scope="col">说明</th>
              </tr>
            </thead>
            <tbody>
              {retention.map((rule) => (
                <tr key={rule.data_class}>
                  <th scope="row">{rule.data_class}</th>
                  <td>{rule.retention}</td>
                  <td>{rule.note ?? "不适用"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="fp-privacy__hint">留存说明：不适用（服务端未返回留存规则，请以客服口径为准）。</p>
        )}
      </section>

      <p className="fp-privacy__isolation">
        会话隔离：切换账号会清除本机的会话缓存与只读视图缓存，另一个用户看不到上一个用户的数据；
        运行模式（live / offline_replay / mock）在页面顶部始终标注。
      </p>
    </section>
  );
}