/** Typed, assignment-bound reporting for an interactive factory worker. */
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function atomicWrite(destination: string, value: unknown): void {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const temporary = `${destination}.${process.pid}.tmp`;
  const fd = fs.openSync(temporary, "w", 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(value, null, 2));
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(temporary, destination);
  const directory = fs.openSync(path.dirname(destination), "r");
  try {
    fs.fsyncSync(directory);
  } finally {
    fs.closeSync(directory);
  }
}

function canonical(value: any): string {
  const sorted = (item: any): any => {
    if (Array.isArray(item)) return item.map(sorted);
    if (item && typeof item === "object") {
      return Object.fromEntries(
        Object.keys(item).sort().map((key) => [key, sorted(item[key])]),
      );
    }
    return item;
  };
  return JSON.stringify(sorted(value));
}

export default function (pi: ExtensionAPI): void {
  const source = process.env.PLUTO_FACTORY_ASSIGNMENT;
  if (!source) {
    throw new Error("Factory extension requires an assignment binding");
  }
  let assignment = JSON.parse(fs.readFileSync(source, "utf8"));
  const schema = JSON.parse(fs.readFileSync(assignment.schema_path, "utf8"));
  const roleDigest = crypto.createHash("sha256")
    .update(fs.readFileSync(assignment.role_path)).digest("hex");
  const initialHome = fs.realpathSync(assignment.home);
  const initialDirectory = fs.realpathSync(assignment.directory);
  const initialWork = assignment.work_id;
  const initialRole = assignment.role;
  const rootIdentity = (value: any) => JSON.stringify(
    value.roots.map((root: any) => ({
      name: root.name,
      path: fs.realpathSync(root.path),
      access: root.access || "write",
    })).sort((left: any, right: any) => left.name.localeCompare(right.name)),
  );
  const initialRoots = rootIdentity(assignment);
  let reported = fs.existsSync(assignment.report_path);
  let requestedReport = false;
  let nativeSession: string | undefined;
  const identity = () => ({
    assignment_id: assignment.id,
    generation: assignment.generation,
  });
  const status = (state: string, extra: object = {}) => {
    atomicWrite(assignment.status_path, {
      ...identity(), state, has_report: reported, ...extra,
    });
  };

  pi.registerCommand("factory-bind", {
    description: "Bind a newer coordinator assignment to this settled session",
    handler: async (argumentsText, ctx) => {
      if (!ctx.isIdle() || ctx.hasPendingMessages()) {
        throw new Error("Factory binding requires a settled, unqueued session");
      }
      const request = JSON.parse(argumentsText);
      const filename = fs.realpathSync(request.assignment_path);
      const allowed = path.join(initialHome, "assignments") + path.sep;
      if (!filename.startsWith(allowed) ||
          path.basename(filename) !== "assignment.json") {
        throw new Error("Assignment binding is outside this factory");
      }
      const next = JSON.parse(fs.readFileSync(filename, "utf8"));
      const nextRoleDigest = crypto.createHash("sha256")
        .update(fs.readFileSync(next.role_path)).digest("hex");
      const sameScope = fs.realpathSync(next.home) === initialHome &&
        fs.realpathSync(next.directory) === initialDirectory &&
        next.work_id === initialWork && next.role === initialRole &&
        rootIdentity(next) === initialRoots;
      if (!sameScope || nextRoleDigest !== roleDigest ||
          canonical(JSON.parse(fs.readFileSync(next.schema_path, "utf8"))) !==
            canonical(schema)) {
        throw new Error("Changed scope or role requires a fresh worker session");
      }
      if (!Number.isInteger(next.generation) ||
          next.generation <= assignment.generation) {
        throw new Error("Factory binding requires a newer generation");
      }
      assignment = next;
      reported = fs.existsSync(assignment.report_path);
      requestedReport = false;
      atomicWrite(assignment.started_path, {
        ...identity(), bound: true, task_started: false,
        session_file: nativeSession,
      });
      status("idle");
    },
  });

  pi.registerTool({
    name: "factory_report",
    label: "Factory report",
    description: "Persist the final typed result for this factory assignment",
    parameters: schema,
    executionMode: "sequential",
    async execute(_toolCallId, report) {
      const envelope = { ...identity(), report };
      if (reported) {
        const previous = JSON.parse(
          fs.readFileSync(assignment.report_path, "utf8"),
        );
        if (canonical(previous.report) !== canonical(report)) {
          throw new Error("A different final report is already recorded");
        }
      } else {
        atomicWrite(assignment.report_path, envelope);
        reported = true;
      }
      status("reported");
      return {
        content: [{
          type: "text",
          text: "Report persisted; coordinator validation is pending",
        }],
        details: envelope,
        terminate: true,
      };
    },
  });

  pi.on("tool_call", async (event) => {
    if (reported && event.toolName !== "factory_report") {
      return {
        block: true,
        reason: "Assignment report is recorded; await coordinator input",
        terminate: true,
      };
    }
  });

  pi.on("session_start", async (_event, ctx) => {
    nativeSession = ctx.sessionManager.getSessionFile();
    atomicWrite(assignment.started_path, {
      ...identity(), task_started: false,
      session_file: nativeSession,
      model: ctx.model?.id,
    });
    status("idle");
  });
  pi.on("agent_start", async () => {
    atomicWrite(assignment.started_path, {
      ...identity(), task_started: true, session_file: nativeSession,
    });
    status("working");
  });
  pi.on("agent_before_settle", async (event) => {
    if (!reported && !requestedReport) {
      requestedReport = true;
      return {
        entries: [...event.entries, {
          type: "custom_message",
          customType: "factory-report-required",
          content: "The assignment requires a final factory_report tool call. " +
            "Report the actual outcome, including errors or remaining work.",
          display: false,
        }],
        continue: true,
      };
    }
  });
  pi.on("agent_settled", async (event) => {
    status("settled", { aborted: event.aborted });
  });
  pi.on("session_shutdown", async () => status("closed"));
}
