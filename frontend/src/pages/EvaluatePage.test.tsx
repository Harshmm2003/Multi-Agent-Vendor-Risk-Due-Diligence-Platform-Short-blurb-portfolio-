import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { json, mockFetch, on } from "../test/fetchMock";
import EvaluatePage from "./EvaluatePage";
import { ROUTER_FUTURE } from "../routerFuture";

function renderPage() {
  render(
    <MemoryRouter future={ROUTER_FUTURE} initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={<EvaluatePage />} />
        <Route path="/jobs/:jobId" element={<p>job page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

const companySearch = on("GET", "/api/v1/companies/search", () =>
  json(200, { companies: [{ name: "Okta, Inc.", ticker: "OKTA", cik: "0001660134" }] }),
);

async function fillValidForm(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Vendor"), "Okt");
  await user.click(await screen.findByRole("option", { name: /Okta, Inc\./ }));
  await user.type(screen.getByLabelText("Company name"), "Acme Corp");
  await user.type(screen.getByLabelText("Industry"), "Financial Services");
  await user.type(screen.getByLabelText("Contract value (USD)"), "500000");
  await user.type(screen.getByLabelText("Priorities"), "24/7 support{Enter}");
  await user.type(screen.getByLabelText("Dealbreakers"), "no SOC 2 Type 2 report{Enter}");
}

// Typing into a whole form is the slowest thing in the suite; on a busy
// machine a test brushed the default 5 s limit, so allow more headroom.
vi.setConfig({ testTimeout: 15_000 });

afterEach(() => vi.unstubAllGlobals());

test("submits the exact request body the backend expects and opens the job", async () => {
  const fetchMock = mockFetch(
    companySearch,
    on("POST", "/api/v1/evaluate", () =>
      json(202, { job_id: "job-abc", evaluation_id: "eval-1", vendor_name: "Okta, Inc.", status: "pending",
                  message: "", poll_url: "/api/v1/jobs/job-abc" })),
  );
  const user = userEvent.setup({ delay: null });
  renderPage();
  await fillValidForm(user);
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));

  expect(await screen.findByText("job page")).toBeInTheDocument();
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST")!;
  expect(JSON.parse(post[1]!.body as string)).toEqual({
    vendor_name: "Okta, Inc.",
    client_profile: {
      company_name: "Acme Corp", industry: "Financial Services", contract_value: 500000,
      contract_years: 3, risk_appetite: "medium", priorities: ["24/7 support"],
      dealbreakers: ["no SOC 2 Type 2 report"], nice_to_have: [],
    },
    weights: { financial: 0.3, compliance: 0.25, operational: 0.2, reputation: 0.1, strategic: 0.15 },
  });
});

test("shows 422 validation errors from the server next to the fields", async () => {
  mockFetch(
    companySearch,
    on("POST", "/api/v1/evaluate", () =>
      json(422, {
        detail: [
          { type: "value_error", loc: ["body", "weights"], msg: "Value error, weights must sum to 1.0, got 1.0500" },
          { type: "float_parsing", loc: ["body", "client_profile", "contract_value"], msg: "Input should be a valid number" },
          { type: "extra", loc: ["body", "client_profile", "something_else"], msg: "Unexpected field" },
        ],
      })),
  );
  const user = userEvent.setup({ delay: null });
  renderPage();
  await fillValidForm(user);
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));

  expect(await screen.findByText("weights must sum to 1.0, got 1.0500")).toBeInTheDocument();
  expect(screen.getByText("Input should be a valid number")).toBeInTheDocument();
  expect(screen.getByText(/client_profile\.something_else: Unexpected field/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start evaluation" })).toBeEnabled();
});

test("does not submit when weights don't sum to 1", async () => {
  const fetchMock = mockFetch(companySearch);
  const user = userEvent.setup({ delay: null });
  renderPage();
  await fillValidForm(user);
  const financial = screen.getByLabelText("Financial");
  await user.clear(financial);
  await user.type(financial, "0.5");

  expect(screen.getByTestId("weights-total")).toHaveTextContent("Total: 1.20");
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));
  expect(screen.getByRole("alert")).toHaveTextContent(/must add up to 1\.00/);
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

test("requires vendor, company, industry and contract value", async () => {
  const fetchMock = mockFetch(companySearch);
  const user = userEvent.setup({ delay: null });
  renderPage();
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));
  expect(screen.getByText("Enter the vendor to evaluate.")).toBeInTheDocument();
  expect(screen.getByText("Enter your company name.")).toBeInTheDocument();
  expect(screen.getByText("Enter your industry.")).toBeInTheDocument();
  expect(screen.getByText("Enter a contract value greater than 0.")).toBeInTheDocument();
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

test("reports an unreachable API", async () => {
  mockFetch(companySearch, on("POST", "/api/v1/evaluate", () => {
    throw new TypeError("Failed to fetch");
  }));
  const user = userEvent.setup({ delay: null });
  renderPage();
  await fillValidForm(user);
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Could not reach the VendorIQ API/));
});

test("sends financial exposure details for the FAIR agent when given", async () => {
  const fetchMock = mockFetch(
    companySearch,
    on("POST", "/api/v1/evaluate", () =>
      json(202, { job_id: "job-abc", evaluation_id: "eval-1", vendor_name: "Okta, Inc.", status: "pending",
                  message: "", poll_url: "/api/v1/jobs/job-abc" })),
  );
  const user = userEvent.setup({ delay: null });
  renderPage();
  await fillValidForm(user);
  await user.click(screen.getByText("Financial exposure details (optional)"));
  await user.click(screen.getByLabelText("Health data (PHI)"));
  await user.selectOptions(screen.getByLabelText("How critical is the service?"), "high");
  await user.type(screen.getByLabelText("Records the vendor will hold"), "200000");
  await user.type(screen.getByLabelText("Annual loss you can absorb (USD)"), "250000");
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));

  expect(await screen.findByText("job page")).toBeInTheDocument();
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST")!;
  expect(JSON.parse(post[1]!.body as string).client_profile.exposure).toEqual({
    data_types: ["phi"],
    service_criticality: "high",
    records_shared: 200000,
    risk_tolerance_usd: 250000,
  });
});

test("rejects a zero tolerance or fractional record count before submitting", async () => {
  const fetchMock = mockFetch(companySearch);
  const user = userEvent.setup({ delay: null });
  renderPage();
  await fillValidForm(user);
  await user.click(screen.getByText("Financial exposure details (optional)"));
  await user.type(screen.getByLabelText("Annual loss you can absorb (USD)"), "0");
  await user.type(screen.getByLabelText("Records the vendor will hold"), "10.5");
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));

  expect(screen.getByText("Enter an amount greater than 0.")).toBeInTheDocument();
  expect(screen.getByText("Enter a whole number.")).toBeInTheDocument();
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

const accepted = on("POST", "/api/v1/evaluate", () =>
  json(202, { job_id: "job-abc", evaluation_id: "eval-1", vendor_name: "Okta, Inc.", status: "pending",
              message: "", poll_url: "/api/v1/jobs/job-abc" }));

test("demo mode locks live runs until a passcode is entered, then sends it", async () => {
  sessionStorage.clear();
  const fetchMock = mockFetch(
    companySearch,
    on("GET", "/api/v1/access", () => json(200, { passcode_enabled: true, public_runs_left: 0, daily_limit: null })),
    accepted,
  );
  const user = userEvent.setup({ delay: null });
  renderPage();
  expect(await screen.findByRole("heading", { name: "Live evaluations are locked on this demo" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Browse the saved reports" })).toHaveAttribute("href", "/history");
  await fillValidForm(user);
  expect(screen.getByRole("button", { name: "Start evaluation" })).toBeDisabled();

  await user.type(screen.getByLabelText("Passcode"), "let-me-in");
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));
  expect(await screen.findByText("job page")).toBeInTheDocument();
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST")!;
  expect((post[1]!.headers as Record<string, string>)["X-Evaluate-Passcode"]).toBe("let-me-in");
});

test("a daily allowance shows how many free runs are left and needs no passcode", async () => {
  sessionStorage.clear();
  const fetchMock = mockFetch(
    companySearch,
    on("GET", "/api/v1/access", () => json(200, { passcode_enabled: true, public_runs_left: 2, daily_limit: 3 })),
    accepted,
  );
  const user = userEvent.setup({ delay: null });
  renderPage();
  expect(await screen.findByRole("heading", { name: "2 free evaluations left today" })).toBeInTheDocument();
  await fillValidForm(user);
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));
  expect(await screen.findByText("job page")).toBeInTheDocument();
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST")!;
  expect(post[1]!.headers as Record<string, string>).not.toHaveProperty("X-Evaluate-Passcode");
});

test("shows the server's message when the allowance runs out mid-session", async () => {
  sessionStorage.clear();
  mockFetch(
    companySearch,
    on("GET", "/api/v1/access", () => json(200, { passcode_enabled: false, public_runs_left: 1, daily_limit: 1 })),
    on("POST", "/api/v1/evaluate", () => json(429, { detail: "Today's free evaluations are used up." })),
  );
  const user = userEvent.setup({ delay: null });
  renderPage();
  await screen.findByRole("heading", { name: "1 free evaluation left today" });
  expect(screen.queryByLabelText(/Passcode/)).not.toBeInTheDocument();
  await fillValidForm(user);
  await user.click(screen.getByRole("button", { name: "Start evaluation" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Today's free evaluations are used up."));
});
