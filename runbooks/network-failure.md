# Network Connectivity Failure

## Symptoms
- Connection refused, resets, or DNS resolution errors between services.
- Elevated 5xx from callers; endpoints missing from a Service.

## Evidence to collect
- Service endpoints and selector labels.
- DNS resolution results from within the cluster.
- Network policy changes and recent rollouts.

## Investigation
1. Verify Service selectors match pod labels.
2. Test name resolution and port reachability from a sibling pod.
3. Review recent network policy or CNI changes.

## Safe response
- Network changes can widen blast radius; require human approval.
- Do not assume the application is at fault until connectivity is verified.
