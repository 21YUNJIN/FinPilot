import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { restoreSession } from "@finpilot/shared-ui";
import { App } from "./App";
import "./styles.css";

// 刷新页面后尝试恢复标签页内的会话（过期则自动清理，不展示上一个用户的数据）。
restoreSession();

const container = document.getElementById("root");
if (!container) throw new Error("找不到 #root 挂载点");
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);