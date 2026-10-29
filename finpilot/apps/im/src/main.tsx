import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { configureClient, restoreSession } from "@finpilot/shared-ui";
import { App } from "./App";
import "./styles.css";

configureClient({ channel: "im" });
restoreSession();

const container = document.getElementById("root");
if (!container) throw new Error("找不到 #root 挂载点");
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);