import { Layout } from "nextra-theme-docs";

import { Footer } from "@/components/landing/footer";
import { Header } from "@/components/landing/header";
import { getBlogIndexData } from "@/core/blog";
import { env } from "@/env";
import "nextra-theme-docs/style.css";

export default async function BlogLayout({ children }) {
  const { pageMap } = await getBlogIndexData();
  const repositoryURL = env.NEXT_PUBLIC_REPOSITORY_URL?.replace(/\/$/, "");
  const repositoryProps = repositoryURL
    ? {
        docsRepositoryBase: `${repositoryURL}/tree/main/frontend/src/content`,
      }
    : {
        docsRepositoryBase: "https://example.invalid/semantic-deer-flow",
        editLink: null,
        feedback: { content: null, link: "https://example.invalid" },
      };

  return (
    <Layout
      {...repositoryProps}
      navbar={<Header className="relative max-w-full px-10" homeURL="/" />}
      pageMap={pageMap}
      sidebar={{ defaultOpen: true }}
      footer={<Footer />}
    >
      {children}
    </Layout>
  );
}
