import { proxyGatewayRequest } from "../_gateway-proxy";

type ApiRouteContext = {
  params: Promise<{ path?: string[] }>;
};

async function gatewayPathname({ params }: ApiRouteContext) {
  const path = (await params).path ?? [];
  return `/api/${path.join("/")}`;
}

export async function GET(request: Request, context: ApiRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function POST(request: Request, context: ApiRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function PUT(request: Request, context: ApiRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function PATCH(request: Request, context: ApiRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function DELETE(request: Request, context: ApiRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function OPTIONS(request: Request, context: ApiRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}

export async function HEAD(request: Request, context: ApiRouteContext) {
  return proxyGatewayRequest(request, await gatewayPathname(context));
}
