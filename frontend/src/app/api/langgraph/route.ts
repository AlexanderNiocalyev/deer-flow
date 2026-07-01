import { proxyGatewayRequest } from "../_gateway-proxy";

export async function GET(request: Request) {
  return proxyGatewayRequest(request, "/api");
}

export async function POST(request: Request) {
  return proxyGatewayRequest(request, "/api");
}

export async function PUT(request: Request) {
  return proxyGatewayRequest(request, "/api");
}

export async function PATCH(request: Request) {
  return proxyGatewayRequest(request, "/api");
}

export async function DELETE(request: Request) {
  return proxyGatewayRequest(request, "/api");
}

export async function OPTIONS(request: Request) {
  return proxyGatewayRequest(request, "/api");
}

export async function HEAD(request: Request) {
  return proxyGatewayRequest(request, "/api");
}
