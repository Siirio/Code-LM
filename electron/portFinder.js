/**
 * portFinder.js
 *
 * Finds 4 free TCP ports for CodeLM containers.
 *
 * Strategy:
 *  - Search range: 40000–49000 (below OS ephemeral range 49152–65535)
 *  - Blocklist: well-known ports that could be reserved even when stopped
 *  - Check: TCP bind attempt (not just ping — catches stopped-but-reserved ports)
 *  - Never return the same port twice in one session
 */

'use strict'

const net = require('net')

// ── Blocklist ────────────────────────────────────────────────────────────────
// Ports we never touch regardless of whether they're currently active.
// Covers: databases, message brokers, Docker, common dev servers, etc.
const BLOCKLIST = new Set([
  // PostgreSQL
  5432, 5433, 5434, 5435,
  // MySQL / MariaDB
  3306, 3307,
  // Redis
  6379, 6380,
  // MongoDB
  27017, 27018, 27019,
  // Neo4j
  7474, 7687,
  // Qdrant
  6333, 6334,
  // Elasticsearch
  9200, 9300,
  // Cassandra
  9042, 9160,
  // CouchDB
  5984,
  // RabbitMQ
  5672, 15672,
  // Kafka
  9092, 2181,
  // Docker daemon / registry
  2375, 2376, 5000,
  // etcd
  2379, 2380,
  // Consul
  8500, 8600,
  // Vault
  8200,
  // MinIO
  9000, 9001,
  // InfluxDB
  8086, 8088,
  // Prometheus / Grafana
  9090, 9091, 3000,
  // Common Node/React/Vite dev servers
  3001, 4000, 4200, 5000, 5001, 5173, 8000, 8080, 8081, 8888,
  // CodeLM backend itself
  8765,
  // Jupyter
  8888, 8889,
  // HTTP / HTTPS
  80, 443, 8443,
  // SSH
  22,
])

const SEARCH_START = 40000
const SEARCH_END   = 49000  // exclusive upper bound

/**
 * Returns true if the port is free to bind on localhost.
 * Uses a real TCP bind — catches ports reserved by stopped services.
 */
function isPortFree(port) {
  return new Promise(resolve => {
    const server = net.createServer()
    server.once('error', () => resolve(false))
    server.once('listening', () => {
      server.close(() => resolve(true))
    })
    server.listen(port, '127.0.0.1')
  })
}

/**
 * Find `count` free ports in the search range, avoiding the blocklist and
 * any ports already chosen in this call (no duplicates).
 *
 * @param {number} count   Number of ports to find (default 4)
 * @returns {Promise<number[]>}
 */
async function findFreePorts(count = 4) {
  const chosen = []
  const chosenSet = new Set()

  for (let port = SEARCH_START; port < SEARCH_END && chosen.length < count; port++) {
    if (BLOCKLIST.has(port)) continue
    if (chosenSet.has(port)) continue

    const free = await isPortFree(port)
    if (free) {
      chosen.push(port)
      chosenSet.add(port)
    }
  }

  if (chosen.length < count) {
    throw new Error(
      `Could not find ${count} free ports in range ${SEARCH_START}–${SEARCH_END}. ` +
      `Only found ${chosen.length}.`
    )
  }

  return chosen
}

/**
 * Find ports and return a named object for CodeLM services.
 *
 * @returns {Promise<{postgres: number, neo4jBolt: number, neo4jBrowser: number, qdrant: number}>}
 */
async function findCodeLMPorts() {
  const [postgres, neo4jBolt, neo4jBrowser, qdrant] = await findFreePorts(4)
  return { postgres, neo4jBolt, neo4jBrowser, qdrant }
}

module.exports = { findCodeLMPorts, isPortFree }
