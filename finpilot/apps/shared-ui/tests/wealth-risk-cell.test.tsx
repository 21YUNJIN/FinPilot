import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { WealthRiskCell } from "../src/components/WealthRiskCell";

describe("remote wealth risk", () => {
  it("shows ratios as percentages and does not replace missing CVaR with zero", () => {
    render(<WealthRiskCell report={{status:"ok", metrics:{
      annual_volatility:{value:0.1234, unit:"ratio_per_year"},
      max_drawdown:{value:-0.052, unit:"ratio"},
      historical_cvar_95:{value:null, unit:"daily_return", na_reason:"insufficient_history"},
    }}} />);
    expect(screen.getByText("年化波动：12.34%")).toBeInTheDocument();
    expect(screen.getByText("观察期最大回撤：-5.20%")).toBeInTheDocument();
    expect(screen.getByText("最差 5% 日收益均值：NA（有效历史样本不足）")).toBeInTheDocument();
  });
  it("keeps dependency failure explicit", () => {
    render(<WealthRiskCell report={{status:"unavailable", message:"服务未连接", metrics:{}}} />);
    expect(screen.getByText("NA（服务未连接）")).toBeInTheDocument();
  });
});
