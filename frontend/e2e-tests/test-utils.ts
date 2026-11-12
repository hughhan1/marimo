/* Copyright 2026 Marimo. All rights reserved. */

import type { Locator, Page } from "@playwright/test";

/**
 * Wait for a marimo app to be fully loaded and ready
 */
export async function waitForMarimoApp(
  page: Page,
  timeout = 30_000,
): Promise<void> {
  await page.waitForLoadState("load", { timeout });

  // Wait for the marimo app to be initialized
  await page.waitForFunction(
    () => {
      if (document.querySelector("[data-testid='marimo-static']") !== null) {
        return true;
      }
      const app = document.querySelector("#App");
      return (
        app?.getAttribute("data-connection-state") === "OPEN" &&
        (document.querySelector("[data-testid='cell-editor']") !== null ||
          document.querySelector(".marimo-cell") !== null)
      );
    },
    { timeout },
  );
}

/**
 * Robust element interaction with retry
 */
export async function clickWithRetry(
  page: Page,
  selector: string,
  maxRetries = 3,
  timeout = 5000,
): Promise<void> {
  let element: Locator | undefined;
  for (let i = 0; i < maxRetries; i++) {
    try {
      element = page.locator(selector);
      await element.waitFor({ state: "visible", timeout });
      break;
    } catch (error) {
      if (i === maxRetries - 1) {
        throw error;
      }
      await page.waitForTimeout(1000);
    }
  }
  if (!element) {
    throw new Error(`Element not found: ${selector}`);
  }
  await element.click({ timeout });
}

export async function hoverWithRetry(
  page: Page,
  selector: string,
  maxRetries = 3,
  timeout = 5000,
): Promise<void> {
  let element: Locator | undefined;
  for (let i = 0; i < maxRetries; i++) {
    try {
      element = page.locator(selector);
      await element.waitFor({ state: "visible", timeout });
      break;
    } catch (error) {
      if (i === maxRetries - 1) {
        throw error;
      }
      await page.waitForTimeout(1000);
    }
  }
  if (!element) {
    throw new Error(`Element not found: ${selector}`);
  }
  await element.hover({ timeout });
}
