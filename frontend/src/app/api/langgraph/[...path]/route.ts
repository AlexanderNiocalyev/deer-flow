import { proxyGatewayRequest } from "../../_gateway-proxy";

type LangGraphRouteContext = {
  params: Promise<{ path?: string[] }>;
};

async function gatewayPathname({ params }: LangGraphRouteContext) {
  const path = (await params).path ?? [];
  return `/api/${path.join("/")}`;
}

export async function GET(request: Request, context: LangGraphRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function POST(request: Request, context: LangGraphRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function PUT(request: Request, context: LangGraphRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function PATCH(request: Request, context: LangGraphRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function DELETE(request: Request, context: LangGraphRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function OPTIONS(request: Request, context: LangGraphRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function HEAD(request: Request, context: LangGraphRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}
