// Compile-time proof that the provider is assignable to the pinned router's MinerCandidateProvider.
import type { MinerCandidateProvider, RouteRequest } from "../../../vendor/flop-session-router/dist/src/index.js";
import { OpenAgentSearchCandidateProvider, OpenAgentSearchLedgerSource } from "../src/index.js";
const provider: MinerCandidateProvider = new OpenAgentSearchCandidateProvider(new OpenAgentSearchLedgerSource(), []);
const request: RouteRequest = { requestId: "typecheck", constraints: { modelId: "example-model" } };
void provider.candidates(request);
