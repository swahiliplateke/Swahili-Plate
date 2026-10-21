const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const baseURL = process.env.TEST_BASE_URL || "http://127.0.0.1:18765";
const customerEmail = process.env.TEST_CUSTOMER_EMAIL;
const customerPhone = process.env.TEST_CUSTOMER_PHONE;
const adminToken = process.env.TEST_ADMIN_TOKEN;
const chromePath = process.env.TEST_CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const riderName = "Playwright Test Rider";
const riderPhone = "0712345678";

function lastChannels(order, event) {
  const record = [...(order.notification_history || [])].reverse().find((item) => item.event === event);
  assert.ok(record, `Expected notification history for ${event}`);
  return record.channels;
}

async function verifyLocalImages(page, selector, baseURL) {
  const urls = await page.locator(selector).evaluateAll((images) =>
    [...new Set(images.map((image) => image.currentSrc || image.src).filter(Boolean))]
  );
  assert.ok(urls.length > 0, `Expected images matching ${selector}`);
  const localURLs = urls.map((url) => new URL(url, baseURL));
  const externalURLs = localURLs.filter((url) => url.origin !== new URL(baseURL).origin).map(String);
  assert.deepEqual(externalURLs, [], "The image check should only request local app assets");
  const results = await Promise.all(localURLs.map(async (url) => {
    const response = await page.request.get(url.href);
    return { url: url.href, status: response.status() };
  }));
  const failures = results.filter((result) => result.status !== 200);
  assert.deepEqual(failures, [], `Broken images: ${JSON.stringify(failures)}`);
  return urls.length;
}

async function main() {
  assert.ok(customerEmail, "Set TEST_CUSTOMER_EMAIL to the customer email for this run");
  assert.ok(customerPhone, "Set TEST_CUSTOMER_PHONE to the customer phone for this run");
  assert.ok(adminToken, "Set TEST_ADMIN_TOKEN to the isolated test server's admin token");

  const browser = await chromium.launch({ headless: false, executablePath: chromePath });
  const page = await browser.newPage();
  page.setDefaultTimeout(12000);
  page.setDefaultNavigationTimeout(15000);
  const pageErrors = [];
  const failedBrandAssets = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.url().includes("/brand-assets/") && response.status() >= 400) {
      failedBrandAssets.push({ url: response.url(), status: response.status() });
    }
  });
  await page.route("**/*.mp4", (route) => route.abort());

  try {
    const health = await page.request.get(`${baseURL}/api/health`);
    assert.equal(health.status(), 200, "The isolated API should be healthy");

    const notificationStatusResponse = await page.request.get(`${baseURL}/api/notifications/status`, {
      headers: { "X-Admin-Token": adminToken },
    });
    assert.equal(notificationStatusResponse.status(), 200);
    const notificationStatus = await notificationStatusResponse.json();
    assert.deepEqual(notificationStatus, { email_configured: false, whatsapp_configured: false },
      "Test server must not be configured to contact real email or WhatsApp accounts");

    await page.goto(`${baseURL}/customer/`, { waitUntil: "commit" });
    await page.locator('#desktop-nav button[data-route="food"]').waitFor({ state: "visible" });
    await page.locator('#desktop-nav button[data-route="food"]').click();
    await page.locator("#food-grid .menu-card").first().waitFor();
    console.log("Customer menu opened");

    const menuPhotos = page.locator("#food-grid img.menu-product-photo");
    assert.ok(await menuPhotos.count(), "Customer menu should include product images");
    await menuPhotos.first().scrollIntoViewIfNeeded();
    await page.waitForFunction(() => {
      const image = document.querySelector("#food-grid img.menu-product-photo");
      return image && image.complete && image.naturalWidth > 0;
    }, null, { timeout: 10000 });
    console.log("Customer product photo loaded");

    await page.locator('#food-tabs button[data-category="All"]').click();
    await page.locator("#food-grid .menu-card").first().waitFor({ state: "visible" });
    const foodPhotoCount = await verifyLocalImages(page, "#food-grid img.menu-product-photo", baseURL);

    await page.locator('#desktop-nav button[data-route="branches"]').click();
    const branchPhotos = page.locator("#branch-cards img");
    await branchPhotos.first().waitFor({ state: "visible" });
    const branchPhotoCount = await branchPhotos.count();
    for (let index = 0; index < branchPhotoCount; index += 1) {
      await branchPhotos.nth(index).scrollIntoViewIfNeeded();
    }
    await page.waitForFunction(() => {
      const images = [...document.querySelectorAll("#branch-cards img")];
      return images.length > 0 && images.every((image) => image.complete && image.naturalWidth > 0);
    }, null, { timeout: 10000 });
    assert.ok(branchPhotoCount >= 2, "Both branches should have a photo");
    console.log("Branch photos loaded");

    await page.locator('#desktop-nav button[data-route="drinks"]').click();
    await page.locator('#drinks-tabs button[data-category="All"]').click();
    await page.locator("#drinks-grid .menu-card").first().waitFor({ state: "visible" });
    const drinkPhotoCount = await verifyLocalImages(page, "#drinks-grid img.menu-product-photo", baseURL);

    await page.locator('#desktop-nav button[data-route="food"]').click();
    await page.locator("#food-grid .menu-card").first().waitFor({ state: "visible" });

    const firstCard = page.locator("#food-grid .menu-card").first();
    const itemName = (await firstCard.locator("h2").innerText()).trim();
    await firstCard.locator(".add-menu-item").click();
    await page.getByRole("button", { name: "Open cart" }).click();
    await page.locator("#checkout-form").waitFor({ state: "visible" });
    console.log("Cart opened for checkout");

    await page.locator("#checkout-name").fill("Omar Lattif");
    await page.locator("#checkout-phone").fill(customerPhone);
    await page.locator("#checkout-email").fill(customerEmail);
    await page.locator("#checkout-whatsapp-opt-in").check();
    await page.locator("#checkout-mode").selectOption({ label: "Delivery" });
    await page.locator("#checkout-branch").selectOption({ label: "CBD" });
    await page.locator("#checkout-payment").selectOption({ label: "Cash on collection / in person" });
    await page.locator("#delivery-address").fill("Playwright test address");
    await page.locator("#delivery-area").fill("Test area");
    await page.locator("#delivery-landmark").fill("Playwright test landmark");
    const cartState = await page.locator("#cart-items").innerText();
    const cartCount = await page.locator("#cart-count").innerText();
    assert.equal(cartCount, "1", `Expected one cart item (${itemName}); got ${cartCount}: ${cartState}`);
    const formState = await page.locator("#checkout-form").evaluate((form) => ({
      valid: form.checkValidity(),
      invalid: [...form.querySelectorAll(":invalid")].map((field) => ({ id: field.id, value: field.value })),
    }));
    assert.equal(formState.valid, true, `Checkout validation failed: ${JSON.stringify(formState.invalid)}`);

    const createResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/api/orders") && response.request().method() === "POST");
    await page.locator("#checkout-submit").click();
    const createResponse = await createResponsePromise;
    assert.equal(createResponse.status(), 201, "Customer checkout should create the test order");
    const createdOrder = await createResponse.json();
    const orderId = createdOrder.id;
    assert.ok(orderId, "Created order should have a server generated ID");
    assert.equal(createdOrder.customer_email, customerEmail);
    assert.equal(createdOrder.customer_phone, customerPhone);
    assert.equal(createdOrder.whatsapp_updates_opt_in, true);
    assert.deepEqual(lastChannels(createdOrder, "New"), { email: "not_configured", whatsapp: "not_configured" });
    await page.waitForFunction(() =>
      document.getElementById("status-message")?.textContent.includes("Order submitted!")
    );
    console.log("Customer checkout submitted");

    await page.goto(`${baseURL}/admin/`, { waitUntil: "commit" });
    await page.locator("#admin-login-dialog").waitFor({ state: "visible" });
    await page.locator("#admin-token-input").fill(adminToken);
    await page.locator("#admin-login-form button[type=submit]").click();

    const orderRow = page.locator("#orders .order-row").filter({ hasText: orderId.slice(-6).toUpperCase() });
    await orderRow.waitFor();
    await orderRow.click();
    console.log("Order opened in admin");
    await page.locator("#rider-name").fill(riderName);
    await page.locator("#rider-phone").fill(riderPhone);

    const updateResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith(`/api/orders/${orderId}`) && response.request().method() === "PATCH");
    await page.getByRole("button", { name: "Assign Rider & Dispatch Order" }).click();
    const updateResponse = await updateResponsePromise;
    assert.equal(updateResponse.status(), 200, "Admin should assign the rider and dispatch the order");
    const dispatchedOrder = await updateResponse.json();
    assert.equal(dispatchedOrder.rider_name, riderName);
    assert.equal(dispatchedOrder.rider_phone, riderPhone);
    assert.equal(dispatchedOrder.order_status, "Out for delivery");
    assert.deepEqual(lastChannels(dispatchedOrder, "Out for delivery"), { email: "not_configured", whatsapp: "not_configured" });
    console.log("Rider assigned and order dispatched");

    await page.goto(`${baseURL}/customer/`, { waitUntil: "commit" });
    await page.locator('#desktop-nav button[data-route="branches"]').waitFor({ state: "visible" });
    await page.locator('#desktop-nav button[data-route="branches"]').click();
    await page.locator("#delivery-tracker-form").waitFor({ state: "visible" });
    await page.locator("#delivery-tracker-phone").fill(customerPhone);
    const trackingPromise = page.waitForResponse((response) =>
      response.url().includes("/api/deliveries/") && response.request().method() === "GET");
    await page.getByRole("button", { name: "Track delivery" }).click();
    const trackingResponse = await trackingPromise;
    assert.equal(trackingResponse.status(), 200, "Customer delivery tracking should find the order");
    const trackedOrder = await trackingResponse.json();
    assert.equal(trackedOrder.rider_name, riderName);
    assert.equal(trackedOrder.rider_phone, riderPhone);
    assert.equal(trackedOrder.order_status, "Out for delivery");
    assert.equal(trackedOrder.customer_phone, customerPhone);

    const trackerText = await page.locator("#delivery-tracker-result").innerText();
    assert.ok(trackerText.includes(riderName), "Customer tracker should show rider name");
    assert.ok(trackerText.includes(riderPhone), "Customer tracker should show rider phone");
    assert.ok(trackerText.includes("Out for delivery"), "Customer tracker should show dispatch status");
    assert.deepEqual(pageErrors, [], `Browser JavaScript errors: ${pageErrors.join("; ")}`);
    assert.deepEqual(failedBrandAssets, [], `Broken brand assets: ${JSON.stringify(failedBrandAssets)}`);

    console.log(JSON.stringify({
      result: "passed",
      orderId,
      menuItem: itemName,
      productImageLoaded: true,
      foodImagesChecked: foodPhotoCount,
      drinkImagesChecked: drinkPhotoCount,
      branchImagesLoaded: branchPhotoCount,
      checkoutAccepted: true,
      riderAssignmentVisibleToCustomer: true,
      notificationProviders: notificationStatus,
      notificationHistory: {
        received: lastChannels(createdOrder, "New"),
        dispatched: lastChannels(dispatchedOrder, "Out for delivery"),
      },
    }, null, 2));
    await page.waitForTimeout(5000);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
