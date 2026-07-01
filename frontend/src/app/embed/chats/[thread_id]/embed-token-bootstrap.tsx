"use client";

import { useEffect } from "react";

import { consumeEmbedTokenFromUrl } from "@/core/embed-auth";

export function EmbedTokenBootstrap() {
  useEffect(() => {
    consumeEmbedTokenFromUrl();
  }, []);

  return null;
}
