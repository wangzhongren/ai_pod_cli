export class ProcessOrderService {
  execute(context: { get(key: string): unknown }) {
    const orderId = context.get("orderId");
    if (typeof orderId !== "string" || !orderId) {
      throw new Error("orderId is required");
    }
    return { processedOrderId: orderId };
  }
}
