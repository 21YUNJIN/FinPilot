/**
 * 风险测评问卷（S3）。
 *
 * 规则：
 * - 六道题必须逐题作答，界面不提供"默认全选"或"一键通过"；
 * - 提交只把答案交给服务端，风险等级由服务端版本化规则评分，前端不计算、不显示预测等级；
 * - 提交中禁用按钮，失败只显示服务端安全文案，不伪造成功。
 */
import { useMemo, useState, type ReactElement } from "react";

import type { RiskQuestionnaireData } from "../types";

export interface RiskQuestionnaireProps {
  questionnaire: RiskQuestionnaireData;
  onSubmit: (answers: Record<string, string>) => void | Promise<void>;
  busy?: boolean;
  error?: string | null;
  onCancel?: () => void;
  className?: string;
  "data-testid"?: string;
}

export function RiskQuestionnaire({
  questionnaire,
  onSubmit,
  busy = false,
  error = null,
  onCancel,
  className,
  "data-testid": dataTestId = "risk-questionnaire",
}: RiskQuestionnaireProps): ReactElement {
  const [answers, setAnswers] = useState<Record<string, string>>({});

  const answered = useMemo(
    () => questionnaire.questions.filter((question) => answers[question.question_id]).length,
    [answers, questionnaire.questions],
  );
  const complete = answered === questionnaire.questions.length;

  return (
    <section className={cx("fp-card", "fp-questionnaire", className)} aria-label="风险测评问卷" data-testid={dataTestId}>
      <header className="fp-card__header">
        <h2 className="fp-card__title">风险测评（{questionnaire.questions.length} 题）</h2>
        <span className="fp-questionnaire__progress" data-testid="questionnaire-progress">
          已答 {answered}/{questionnaire.questions.length}
        </span>
      </header>

      <p className="fp-questionnaire__notice">
        本测评为本项目版本化模拟规则，用于演示适当性约束，不代表任何银行的正式风险测评结论。
        风险等级由服务端按答案评分，对话与前端都不直接决定等级。
      </p>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (complete && !busy) {
            void onSubmit(answers);
          }
        }}
      >
        {questionnaire.questions.map((question, index) => (
          <fieldset key={question.question_id} className="fp-questionnaire__question" data-testid={`question-${question.dimension}`}>
            <legend>
              {index + 1}. {question.text}
              <span className="fp-questionnaire__dimension">（{question.dimension_label}）</span>
            </legend>
            {question.options.map((option) => (
              <label key={option.option_id} className="fp-questionnaire__option">
                <input
                  type="radio"
                  name={question.question_id}
                  value={option.option_id}
                  checked={answers[question.question_id] === option.option_id}
                  onChange={() =>
                    setAnswers((current) => ({ ...current, [question.question_id]: option.option_id }))
                  }
                />
                <span>{option.label}</span>
              </label>
            ))}
          </fieldset>
        ))}

        {error ? (
          <p className="fp-questionnaire__error" role="alert" data-testid="questionnaire-error">
            {error}
          </p>
        ) : null}

        <div className="fp-questionnaire__actions">
          <button
            type="submit"
            className="fp-button fp-button--primary"
            disabled={!complete || busy}
            data-testid="questionnaire-submit"
          >
            {busy ? "提交中…" : complete ? "提交测评" : "答完所有题目后可提交"}
          </button>
          {onCancel ? (
            <button type="button" className="fp-button" onClick={onCancel} disabled={busy}>
              取消
            </button>
          ) : null}
        </div>
      </form>
    </section>
  );
}

function cx(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}