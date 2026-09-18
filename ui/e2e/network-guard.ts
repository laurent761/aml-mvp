export type HttpFailure = { method: string; path: string; status: number };
type ExpectedFailure = HttpFailure & { reason: string; min?: number; max?: number };

/** Classify errors when they occur; expectations cannot excuse earlier failures. */
export class NetworkGuard {
  private expectations: (ExpectedFailure & { seen: number; min: number; max: number })[] = [];
  readonly responses: (HttpFailure & { expected: boolean; reason?: string })[] = [];
  readonly transportFailures: { method: string; path: string; error: string }[] = [];
  readonly cancellations: { method: string; path: string; error: string }[] = [];
  readonly pageErrors: string[] = [];

  expectHttp(expected: ExpectedFailure) {
    const { min = 1, max = 1 } = expected;
    if (!expected.reason.trim() || !Number.isInteger(min) || !Number.isInteger(max) || min < 0 || max < min) {
      throw new Error("Expected HTTP failures require a reason and a valid bounded occurrence count");
    }
    this.expectations.push({ ...expected, min, max, seen: 0 });
  }

  recordHttp(failure: HttpFailure) {
    if (failure.status < 400) return;
    const rule = this.expectations.find(item => item.method === failure.method && item.path === failure.path && item.status === failure.status && item.seen < item.max);
    if (rule) rule.seen += 1;
    this.responses.push({ ...failure, expected: Boolean(rule), ...(rule ? { reason: rule.reason } : {}) });
  }

  recordTransport(failure: { method: string; path: string; error: string }) {
    // Browsers emit these when AbortController cancels work on navigation/unmount.
    // They remain visible in the report and are never counted as HTTP successes.
    if (/^(net::ERR_ABORTED|NS_BINDING_ABORTED|cancelled|canceled)$/.test(failure.error)) {
      this.cancellations.push(failure);
    } else {
      this.transportFailures.push(failure);
    }
  }

  violations() {
    return [
      ...this.responses.filter(item => !item.expected).map(item => `Unexpected HTTP ${item.status}: ${item.method} ${item.path}`),
      ...this.transportFailures.map(item => `Transport failure: ${item.method} ${item.path}: ${item.error}`),
      ...this.pageErrors.map(error => `Uncaught browser exception: ${error}`),
      ...this.expectations.filter(item => item.seen < item.min).map(item => `Expected HTTP ${item.status}: ${item.method} ${item.path} occurred ${item.seen} times; expected ${item.min}–${item.max} (${item.reason})`),
    ];
  }

  report() {
    return { responses: this.responses, transportFailures: this.transportFailures, cancellations: this.cancellations, pageErrors: this.pageErrors, expectations: this.expectations, violations: this.violations() };
  }
}
