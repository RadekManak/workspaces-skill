import { execFile } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { promisify } from "node:util";
import * as sandcastle from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";

const execFileAsync = promisify(execFile);

type PlanIssue = {
  id: string;
  title: string;
  branch: string;
  body: string;
  path: string;
};

type Plan = {
  workspace: {
    id: string;
    title?: string;
    specPath: string;
  };
  repo: {
    name?: string;
    worktreePath: string;
    branch?: string;
  };
  issues: PlanIssue[];
};

type ReviewDecision = {
  status: "approved" | "needs-fix" | "blocked-hitl";
  feedback: string;
};

type IssueResult = {
  id: string;
  status: "merged" | "needs-fix" | "blocked-hitl" | "failed";
  reviewStatus: "approved" | "needs-fix" | "blocked-hitl" | "failed";
  branch: string;
  commits: { sha: string }[];
  cleanupStatus: "pending";
  mergeApproved?: boolean;
  mergeConflict?: boolean;
  note?: string;
  lastError?: string;
  reviewFeedback?: string;
};

const requiredEnv = (name: string): string => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
};

const PLAN_PATH = requiredEnv("WORKSPACE_SANDCASTLE_PLAN");
const RESULT_PATH = requiredEnv("WORKSPACE_SANDCASTLE_RESULT");
const IMPLEMENT_MODEL =
  process.env.SANDCASTLE_IMPLEMENT_MODEL ?? "claude-sonnet-4-6";
const REVIEW_MODEL = process.env.SANDCASTLE_REVIEW_MODEL ?? "claude-sonnet-4-6";
const MAX_REVIEW_CYCLES = Number(process.env.SANDCASTLE_MAX_REVIEW_CYCLES ?? "2");
const IMPLEMENT_MAX_ITERATIONS = Number(
  process.env.SANDCASTLE_IMPLEMENT_MAX_ITERATIONS ?? "100",
);
const SETUP_COMMAND = process.env.SANDCASTLE_SETUP_COMMAND;

const agent = (model: string) => {
  const provider = process.env.SANDCASTLE_AGENT_PROVIDER ?? "claude";
  if (provider === "codex") return sandcastle.codex(model);
  if (provider === "claude") return sandcastle.claudeCode(model);
  throw new Error(`Unsupported SANDCASTLE_AGENT_PROVIDER: ${provider}`);
};

const extractReview = (stdout: string): ReviewDecision => {
  const match = stdout.match(/<review>\s*([\s\S]*?)\s*<\/review>/);
  if (!match) {
    return {
      status: "needs-fix",
      feedback: "Reviewer did not emit a <review> JSON block.",
    };
  }
  try {
    const parsed = JSON.parse(match[1]!) as Partial<ReviewDecision>;
    if (
      parsed.status === "approved" ||
      parsed.status === "needs-fix" ||
      parsed.status === "blocked-hitl"
    ) {
      return {
        status: parsed.status,
        feedback: String(parsed.feedback ?? ""),
      };
    }
  } catch {
    // Fall through to needs-fix below.
  }
  return {
    status: "needs-fix",
    feedback: "Reviewer emitted invalid review JSON.",
  };
};

const mergeBranch = async (branch: string) => {
  try {
    await execFileAsync("git", ["merge", branch, "--no-edit"], {
      cwd: process.cwd(),
    });
  } catch (error) {
    await execFileAsync("git", ["merge", "--abort"], {
      cwd: process.cwd(),
    }).catch(() => undefined);
    throw error;
  }
};

const runIssue = async (plan: Plan, issue: PlanIssue): Promise<IssueResult> => {
  const hooks = SETUP_COMMAND
    ? { sandbox: { onSandboxReady: [{ command: SETUP_COMMAND }] } }
    : undefined;

  const sandbox = await sandcastle.createSandbox({
    branch: issue.branch,
    baseBranch: plan.repo.branch,
    sandbox: docker(),
    hooks,
  });

  const commits: { sha: string }[] = [];
  let review: ReviewDecision = {
    status: "needs-fix",
    feedback: "Review has not run yet.",
  };

  try {
    let reviewFeedback = "";
    for (let attempt = 1; attempt <= Math.max(1, MAX_REVIEW_CYCLES); attempt++) {
      const implement = await sandbox.run({
        name: `implement-${issue.id}`,
        maxIterations: IMPLEMENT_MAX_ITERATIONS,
        agent: agent(IMPLEMENT_MODEL),
        promptFile: "./.sandcastle/implement-prompt.md",
        promptArgs: {
          WORKSPACE_ID: plan.workspace.id,
          WORKSPACE_TITLE: plan.workspace.title ?? plan.workspace.id,
          WORKSPACE_SPEC_PATH: plan.workspace.specPath,
          ISSUE_ID: issue.id,
          ISSUE_TITLE: issue.title,
          ISSUE_BODY: issue.body,
          ISSUE_PATH: issue.path,
          BRANCH: issue.branch,
          REVIEW_FEEDBACK: reviewFeedback,
        },
      });
      commits.push(...implement.commits);

      const reviewRun = await sandbox.run({
        name: `review-${issue.id}`,
        maxIterations: 1,
        agent: agent(REVIEW_MODEL),
        promptFile: "./.sandcastle/review-prompt.md",
        promptArgs: {
          WORKSPACE_ID: plan.workspace.id,
          WORKSPACE_TITLE: plan.workspace.title ?? plan.workspace.id,
          WORKSPACE_SPEC_PATH: plan.workspace.specPath,
          ISSUE_ID: issue.id,
          ISSUE_TITLE: issue.title,
          ISSUE_BODY: issue.body,
          ISSUE_PATH: issue.path,
          BRANCH: issue.branch,
        },
      });
      commits.push(...reviewRun.commits);
      review = extractReview(reviewRun.stdout);

      if (review.status === "approved") {
        return {
          id: issue.id,
          status: "merged",
          reviewStatus: "approved",
          branch: issue.branch,
          commits,
          cleanupStatus: "pending",
          mergeApproved: true,
          note: `Issue ${issue.id} approved after ${attempt} review cycle(s).`,
        };
      }

      if (review.status === "blocked-hitl") {
        return {
          id: issue.id,
          status: "blocked-hitl",
          reviewStatus: "blocked-hitl",
          branch: issue.branch,
          commits,
          cleanupStatus: "pending",
          reviewFeedback: review.feedback,
          note: `Issue ${issue.id} needs human input: ${review.feedback}`,
        };
      }

      reviewFeedback = review.feedback;
    }

    return {
      id: issue.id,
      status: "needs-fix",
      reviewStatus: "needs-fix",
      branch: issue.branch,
      commits,
      cleanupStatus: "pending",
      reviewFeedback: review.feedback,
      note: `Issue ${issue.id} still needs fixes after ${MAX_REVIEW_CYCLES} review cycle(s).`,
    };
  } catch (error) {
    return {
      id: issue.id,
      status: "failed",
      reviewStatus: "failed",
      branch: issue.branch,
      commits,
      cleanupStatus: "pending",
      lastError: error instanceof Error ? error.message : String(error),
    };
  } finally {
    await sandbox.close().catch(() => undefined);
  }
};

const plan = JSON.parse(await readFile(PLAN_PATH, "utf-8")) as Plan;
const settled = await Promise.allSettled(
  plan.issues.map((issue) => runIssue(plan, issue)),
);

const issues: IssueResult[] = settled.map((outcome, index) => {
  if (outcome.status === "fulfilled") return outcome.value;
  const issue = plan.issues[index]!;
  return {
    id: issue.id,
    status: "failed",
    reviewStatus: "failed",
    branch: issue.branch,
    commits: [],
    cleanupStatus: "pending",
    lastError:
      outcome.reason instanceof Error
        ? outcome.reason.message
        : String(outcome.reason),
  };
});

for (const issue of issues) {
  if (!issue.mergeApproved) continue;
  try {
    await mergeBranch(issue.branch);
    issue.status = "merged";
    issue.reviewStatus = "approved";
    issue.note = `${issue.note ?? `Issue ${issue.id} approved.`} Merged ${issue.branch}.`;
  } catch (error) {
    issue.status = "blocked-hitl";
    issue.reviewStatus = "approved";
    issue.mergeConflict = true;
    issue.lastError = error instanceof Error ? error.message : String(error);
    issue.note = `Issue ${issue.id} was approved but could not merge ${issue.branch}. Resolve the merge conflict on the workspace task branch or create a follow-up issue.`;
  }
}

for (const issue of issues) {
  delete issue.mergeApproved;
}

await writeFile(
  RESULT_PATH,
  `${JSON.stringify({ version: 1, issues }, null, 2)}\n`,
  "utf-8",
);
