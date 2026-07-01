import { proxyGatewayRequest } from "../../_gateway-proxy";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyGatewayRequest(
    request,
    `/api/memory/${(await params).path.join("/")}`,
  );
}

export async function POST(
  request: Request,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyGatewayRequest(
    request,
    `/api/memory/${(await params).path.join("/")}`,
  );
}

export async function DELETE(
  request: Request,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyGatewayRequest(
    request,
    `/api/memory/${(await params).path.join("/")}`,
  );
}

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyGatewayRequest(
    request,
    `/api/memory/${(await params).path.join("/")}`,
  );
}
