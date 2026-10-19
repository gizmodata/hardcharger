// The part of swagger-ui-dist's self-contained bundle the API page uses (the package has no
// types of its own).
declare module "swagger-ui-dist/swagger-ui-es-bundle.js" {
  interface SwaggerUiSystem {
    getConfigs: () => Record<string, unknown>;
  }
  type SwaggerUiPlugin = () => { wrapComponents?: Record<string, () => () => null> };
  interface SwaggerUiOptions {
    domNode: HTMLElement;
    spec: object;
    presets?: unknown[];
    plugins?: SwaggerUiPlugin[];
    layout?: string;
    deepLinking?: boolean;
    queryConfigEnabled?: boolean;
    validatorUrl?: string | null;
    persistAuthorization?: boolean;
    showMutatedRequest?: boolean;
    displayRequestDuration?: boolean;
    tryItOutEnabled?: boolean;
    filter?: boolean;
    defaultModelsExpandDepth?: number;
    requestInterceptor?: (request: never) => unknown;
    responseInterceptor?: (response: never) => unknown;
  }
  interface SwaggerUiBundle {
    (options: SwaggerUiOptions): SwaggerUiSystem;
    presets: { apis: unknown };
  }
  const SwaggerUIBundle: SwaggerUiBundle;
  export default SwaggerUIBundle;
}
