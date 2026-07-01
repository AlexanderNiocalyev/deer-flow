import { Toaster } from "sonner";

import { ChatProviders } from "@/app/workspace/chats/[thread_id]/providers";
import { QueryClientProvider } from "@/components/query-client-provider";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { AuthProvider } from "@/core/auth/AuthProvider";

import { EmbedTokenBootstrap } from "./embed-token-bootstrap";

export const dynamic = "force-dynamic";

export default function EmbedChatLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <AuthProvider initialUser={null}>
      <QueryClientProvider>
        <SidebarProvider className="h-screen" defaultOpen={false}>
          <SidebarInset className="min-w-0">
            <ChatProviders>
              <EmbedTokenBootstrap />
              {children}
            </ChatProviders>
          </SidebarInset>
        </SidebarProvider>
        <Toaster position="top-center" />
      </QueryClientProvider>
    </AuthProvider>
  );
}
