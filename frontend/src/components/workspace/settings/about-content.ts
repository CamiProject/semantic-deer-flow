import { env } from "@/env";

const repository = env.NEXT_PUBLIC_REPOSITORY_URL;
const repositorySection = repository
  ? `## Repository\n\n[Open the Semantic DeerFlow repository](${repository})`
  : "## Repository\n\nThe downstream repository URL has not been configured for this build.";

export const aboutMarkdown = `# About Semantic DeerFlow

Semantic DeerFlow is an unofficial downstream of [ByteDance DeerFlow](https://github.com/bytedance/deer-flow). It is not affiliated with or endorsed by ByteDance or the official DeerFlow project.

This downstream provides governed SaaS semantic queries, tenant Scope enforcement, approval-gated Actions, and public Fake IAM/Domain evaluation services. Its primary integration surface is the backend API. This frontend is currently intended for development and debugging.

${repositorySection}

## Compatibility

The project retains the upstream \`deerflow.*\` imports, \`DEER_FLOW_*\` environment variables, APIs, core data structures, and Docker compatibility identifiers.

## License And Upstream

Semantic DeerFlow is distributed under the MIT License and preserves the original ByteDance and DeerFlow Authors copyright notices. The official upstream source is [github.com/bytedance/deer-flow](https://github.com/bytedance/deer-flow).
`;
