import { proxyGatewayRequest } from "../_gateway-proxy";

export async function GET(request: Request) {
  return proxyGatewayRequest(request, "/api/memory");
}

export async function DELETE(request: Request) {
  return proxyGatewayRequest(request, "/api/memory");
}
