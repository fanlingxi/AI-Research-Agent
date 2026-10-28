import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../lib/api";
import { ErrorBlock } from "./AsyncState";

describe("client error messages", () => {
  it.each([
    [404, "未找到所需记录"],
    [409, "记录状态已变化"],
    [422, "提交内容未通过校验"],
    [403, "当前没有权限"],
    [429, "请求过于频繁"],
    [418, "请求未能完成"],
  ])("explains HTTP %s in Chinese and keeps the original error in closed details", async (status, message) => {
    render(<ErrorBlock error={new ApiError("Raw backend error.", status)} />);
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    const original = screen.getByText("Raw backend error.");
    expect(original.closest("details")).not.toHaveAttribute("open");
    expect(original).not.toBeVisible();
    await userEvent.click(screen.getByText("详情"));
    expect(original).toBeVisible();
  });
});
