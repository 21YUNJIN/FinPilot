import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PrivacyPanel } from "../src/components/PrivacyPanel";

describe("PrivacyPanel 隐私面板", () => {
  it("提供导出与删除两个真实入口，并展示留存说明", () => {
    render(
      <PrivacyPanel
        retention={[
          { data_class: "交易与授权记录", retention: "5 年", note: "监管留存要求" },
          { data_class: "对话上下文", retention: "30 天", note: "到期自动清理" },
        ]}
        onExport={vi.fn()}
        onDeleteRequest={vi.fn()}
      />,
    );
    expect(screen.getByTestId("privacy-export-button")).toBeInTheDocument();
    expect(screen.getByTestId("privacy-delete-button")).toBeInTheDocument();
    const table = screen.getByTestId("privacy-retention-table");
    expect(within(table).getByText("交易与授权记录")).toBeInTheDocument();
    expect(within(table).getByText("5 年")).toBeInTheDocument();
    expect(within(table).getByText("30 天")).toBeInTheDocument();
  });

  it("删除必须二次确认（输入'删除'后才可用），并且不声称抹除金融事实", async () => {
    const user = userEvent.setup();
    const onDeleteRequest = vi.fn();
    render(<PrivacyPanel onDeleteRequest={onDeleteRequest} />);

    const deleteButton = screen.getByTestId("privacy-delete-button");
    expect(deleteButton).toBeDisabled();
    expect(screen.getByText(/不会被直接抹除/)).toBeInTheDocument();

    await user.type(screen.getByTestId("privacy-delete-confirm"), "删除");
    expect(deleteButton).toBeEnabled();

    await user.selectOptions(screen.getByTestId("privacy-delete-scope"), "preferences");
    await user.click(deleteButton);
    expect(onDeleteRequest).toHaveBeenCalledWith({ scope: "preferences", reason: "" });
    // 提交后清空确认文本，避免误触重复提交
    expect(screen.getByTestId("privacy-delete-confirm")).toHaveValue("");
  });

  it("导出结果只展示服务端返回的凭据，缺失显示'不适用'", () => {
    render(
      <PrivacyPanel
        onExport={vi.fn()}
        exportView={{ request_id: "req_export_1", generated_at: "2026-10-27T08:00:00Z", download_url: null }}
      />,
    );
    const result = screen.getByTestId("privacy-export-result");
    expect(within(result).getByText("req_export_1")).toBeInTheDocument();
    expect(within(result).getByText(/不适用（服务端未返回下载地址）/)).toBeInTheDocument();
  });

  it("说明会话缓存隔离与运行模式标注（SEC-04）", () => {
    render(<PrivacyPanel onDeleteRequest={vi.fn()} />);
    expect(screen.getByText(/切换账号会清除本机的会话缓存/)).toBeInTheDocument();
    expect(screen.getByText(/运行模式（live \/ offline_replay \/ mock）在页面顶部始终标注/)).toBeInTheDocument();
  });
});