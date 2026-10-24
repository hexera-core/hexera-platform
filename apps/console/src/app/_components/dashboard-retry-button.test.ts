import assert from "node:assert/strict";
import test from "node:test";

import { RetryButton } from "./dashboard-retry-button";

test("the retry button invokes the recovery action", () => {
  let retries = 0;
  const button = RetryButton({
    label: "Try again",
    onRetry: () => {
      retries += 1;
    },
  });

  assert.equal(button.type, "button");
  assert.equal(button.props.type, "button");
  assert.equal(button.props.children, "Try again");
  button.props.onClick();
  assert.equal(retries, 1);
});
