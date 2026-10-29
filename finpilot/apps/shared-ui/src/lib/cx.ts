import type { CSSProperties } from "react";

/** 极简 className 合并（避免为两个客户端引入额外依赖）。 */
export function cx(...values: Array<string | false | null | undefined>): string {
  return values.filter((value): value is string => typeof value === "string" && value !== "").join(" ");
}

/** 视觉隐藏但对读屏可见的文本（用于给图标补文字，不改变可见文案）。 */
export function visuallyHiddenStyle(): CSSProperties {
  return {
    position: "absolute",
    width: 1,
    height: 1,
    padding: 0,
    margin: -1,
    overflow: "hidden",
    clip: "rect(0, 0, 0, 0)",
    whiteSpace: "nowrap",
    border: 0,
  };
}