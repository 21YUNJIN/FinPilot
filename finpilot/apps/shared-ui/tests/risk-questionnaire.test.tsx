import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RiskQuestionnaire } from "../src/components/RiskQuestionnaire";
import type { RiskQuestionnaireData } from "../src/types";

const dimensions = [
  ["investment_experience", "投资经验"],
  ["loss_tolerance", "承受损失能力"],
  ["risk_attitude", "风险态度"],
  ["investment_horizon", "投资期限"],
  ["liquidity_need", "流动性需求"],
  ["capital_purpose", "资金用途"],
] as const;

const questionnaire: RiskQuestionnaireData = {
  questionnaire_version: "2026.10",
  validity_days: 365,
  max_score: 24,
  dimensions: dimensions.map(([dimension, label]) => ({ dimension, label })),
  questions: dimensions.map(([dimension, label], index) => ({
    question_id: `q_${dimension}`,
    dimension,
    dimension_label: label,
    text: `第 ${index + 1} 题`,
    options: [0, 1, 2, 3, 4].map((score) => ({
      option_id: `${dimension}_${score}`,
      label: `选项 ${score}`,
      score,
    })),
  })),
};

describe("RiskQuestionnaire", () => {
  it("六道题逐题渲染，未答完不允许提交", () => {
    render(<RiskQuestionnaire questionnaire={questionnaire} onSubmit={vi.fn()} />);
    expect(screen.getByTestId("questionnaire-progress")).toHaveTextContent("已答 0/6");
    expect(screen.getByTestId("questionnaire-submit")).toBeDisabled();
    for (const [dimension] of dimensions) {
      expect(screen.getByTestId(`question-${dimension}`)).toBeInTheDocument();
    }
  });

  it("答完六题后提交，答案按题目 id 回传且不含等级", () => {
    const onSubmit = vi.fn();
    render(<RiskQuestionnaire questionnaire={questionnaire} onSubmit={onSubmit} />);

    for (const [dimension] of dimensions) {
      const fieldset = screen.getByTestId(`question-${dimension}`);
      const option = fieldset.querySelector("input[type=radio]") as HTMLInputElement;
      fireEvent.click(option);
    }

    expect(screen.getByTestId("questionnaire-progress")).toHaveTextContent("已答 6/6");
    const submit = screen.getByTestId("questionnaire-submit");
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const answers = onSubmit.mock.calls[0][0] as Record<string, string>;
    expect(Object.keys(answers)).toHaveLength(6);
    expect(answers).toHaveProperty("q_investment_experience");
    // 前端只回传选项，不计算也不回传风险等级
    expect(JSON.stringify(answers)).not.toMatch(/R[1-5]/);
  });

  it("提交中禁用按钮，错误只显示服务端文案", () => {
    render(
      <RiskQuestionnaire
        questionnaire={questionnaire}
        onSubmit={vi.fn()}
        busy
        error="问卷未答完，无法评分。"
      />,
    );
    expect(screen.getByTestId("questionnaire-submit")).toBeDisabled();
    expect(screen.getByTestId("questionnaire-error")).toHaveTextContent("问卷未答完");
  });
});